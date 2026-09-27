#include "turn.hpp"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <stdexcept>
#include <string>
#include <vector>

#include "text.hpp"
#include "utf8.hpp"

#include "jsonvalue.hpp"

namespace engine {
namespace {

constexpr const char* kIconCompleted = "\u2705";
constexpr const char* kIconError = "\u274c";
constexpr const char* kIconRunning = "\u23f3";
constexpr const char* kIconOther = "\u2022";
constexpr const char* kEllipsis = "\u2026";

double monotonic_seconds() {
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    return std::chrono::duration<double>(now).count();
}

// data.get(key), with an explicit null treated as absent so that `or` defaults
// apply.
const Json* lookup(const Json& obj, const char* key) {
    if (!obj.is_object()) {
        return nullptr;
    }
    const auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) {
        return nullptr;
    }
    return &*it;
}

// `field or ""`
std::string string_or_empty(const Json* value) {
    if (value == nullptr || !value->is_string()) {
        return "";
    }
    return value->get<std::string>();
}

// `int(field or 0)`
long long int_or_zero(const Json* value) {
    if (value == nullptr || !truthy(*value)) {
        return 0;
    }
    if (value->is_number_integer()) {
        return value->get<long long>();
    }
    if (value->is_number_unsigned()) {
        return static_cast<long long>(value->get<unsigned long long>());
    }
    if (value->is_number_float()) {
        return static_cast<long long>(value->get<double>());
    }
    if (value->is_boolean()) {
        return value->get<bool>() ? 1 : 0;
    }
    if (value->is_string()) {
        try {
            return std::stoll(value->get<std::string>());
        } catch (const std::exception&) {
            return 0;
        }
    }
    return 0;
}

// `float(field or 0.0)`
double float_or_zero(const Json* value) {
    if (value == nullptr || !truthy(*value)) {
        return 0.0;
    }
    if (value->is_number()) {
        return value->get<double>();
    }
    if (value->is_boolean()) {
        return value->get<bool>() ? 1.0 : 0.0;
    }
    if (value->is_string()) {
        try {
            return std::stod(value->get<std::string>());
        } catch (const std::exception&) {
            return 0.0;
        }
    }
    return 0.0;
}

// `field or {}` where an object is expected.
Json object_or_empty(const Json* value) {
    if (value == nullptr || !value->is_object()) {
        return Json::object();
    }
    return *value;
}

// str(data.get("error").get("message") or data.get("error"))[:limit]
std::string step_error_text(const Json* value, std::size_t limit) {
    const Json error = value != nullptr ? *value : Json::object();
    std::string text;
    if (error.is_object()) {
        const auto it = error.find("message");
        if (it != error.end() && truthy(*it)) {
            text = py_str(*it);
        } else {
            // Python falls back to str(dict), i.e. a Python repr. JSON is used
            // here instead; only reachable for an error payload with no message.
            text = error.dump();
        }
    } else {
        text = py_str(error);
    }
    return utf8::truncate(text, limit);
}

// str(error.get("message") or error.get("type") or "execution failed")[:limit]
std::string execution_error_text(const Json* value, std::size_t limit) {
    const Json error = value != nullptr ? *value : Json::object();
    std::string text;
    if (error.is_object()) {
        const auto pick = [&error](const char* key) -> std::optional<std::string> {
            const auto it = error.find(key);
            if (it == error.end() || !truthy(*it)) {
                return std::nullopt;
            }
            return py_str(*it);
        };
        if (const auto message = pick("message")) {
            text = *message;
        } else if (const auto type = pick("type")) {
            text = *type;
        } else {
            text = "execution failed";
        }
    } else {
        text = py_str(error);
    }
    return utf8::truncate(text, limit);
}

// Python's " ".join(value.split()).
std::string collapse_whitespace(const std::string& value) {
    const auto cps = utf8::decode(value);
    std::string out;
    std::vector<char32_t> token;
    const auto flush = [&]() {
        if (token.empty()) {
            return;
        }
        if (!out.empty()) {
            out += " ";
        }
        out += utf8::encode(token);
        token.clear();
    };
    for (const char32_t c : cps) {
        if (utf8::is_space(c)) {
            flush();
        } else {
            token.push_back(c);
        }
    }
    flush();
    return out;
}

bool is_blank(const std::string& value) {
    auto cps = utf8::decode(value);
    utf8::strip_in_place(cps);
    return cps.empty();
}

std::string strip_copy(const std::string& value) {
    auto cps = utf8::decode(value);
    utf8::strip_in_place(cps);
    return utf8::encode(cps);
}

std::size_t codepoint_length(const std::string& value) {
    return utf8::decode(value).size();
}

std::shared_ptr<Step> step_started(TurnState& state, const Json& data) {
    const std::string message_id = string_or_empty(lookup(data, "assistantMessageID"));
    auto step = state.step_for(message_id);
    if (step == nullptr) {
        step = std::make_shared<Step>(message_id);
        state.steps.push_back(step);
    }
    state.current = step;
    return step;
}

std::shared_ptr<Step> current_or_started(TurnState& state, const Json& data) {
    return state.current != nullptr ? state.current : step_started(state, data);
}

// Locate the step a step-scoped event refers to.
std::shared_ptr<Step> resolve_step(TurnState& state, const Json& data,
                                   const std::shared_ptr<Step>& fallback) {
    std::string message_id = string_or_empty(lookup(data, "assistantMessageID"));
    if (message_id.empty()) {
        message_id = fallback->message_id;
    }
    if (auto found = state.step_for(message_id)) {
        return found;
    }
    return fallback;
}

std::shared_ptr<ToolActivity> find_tool(TurnState& state, const Json& data) {
    const std::string call_id = string_or_empty(lookup(data, "id"));
    auto step = state.step_for(string_or_empty(lookup(data, "assistantMessageID")));
    if (step == nullptr) {
        step = state.current;
    }
    if (step == nullptr) {
        step = step_started(state, data);
    }

    if (auto existing = step->tool(call_id)) {
        return existing;
    }
    for (auto it = state.steps.rbegin(); it != state.steps.rend(); ++it) {
        if (auto candidate = (*it)->tool(call_id)) {
            return candidate;
        }
    }
    // The stream can report a tool we never saw called (reconnects, dropped
    // events); record it rather than losing the error.
    return step->tool_or_create(call_id);
}

bool is_known_status(const std::string& status) {
    return status == "completed" || status == "error" || status == "running";
}

}  // namespace

long long token_total(const Json& tokens) {
    if (!truthy(tokens)) {
        return 0;
    }

    long long total = 0;
    const auto add = [&total](const Json& obj, const char* key) {
        if (!obj.is_object()) {
            return;
        }
        const auto it = obj.find(key);
        if (it == obj.end() || !truthy(*it)) {
            return;
        }
        if (it->is_number_integer()) {
            total += it->get<long long>();
        } else if (it->is_number_unsigned()) {
            total += static_cast<long long>(it->get<unsigned long long>());
        } else if (it->is_number_float()) {
            total += static_cast<long long>(it->get<double>());
        } else if (it->is_string()) {
            try {
                total += std::stoll(it->get<std::string>());
            } catch (const std::exception&) {
                // int(x) raising is swallowed by the original.
            }
        }
    };

    for (const char* key : {"input", "output", "reasoning"}) {
        add(tokens, key);
    }
    const auto cache = tokens.find("cache");
    if (cache != tokens.end() && truthy(*cache)) {
        for (const char* key : {"read", "write"}) {
            add(*cache, key);
        }
    }
    return total;
}

std::string tool_detail(const Json& payload, std::size_t width) {
    if (!payload.is_object() || payload.empty()) {
        return "";
    }
    for (const char* key : {"command", "filePath", "path", "pattern", "query", "url", "prompt",
                            "description"}) {
        const auto it = payload.find(key);
        if (it == payload.end() || !it->is_string()) {
            continue;
        }
        const std::string raw = it->get<std::string>();
        if (is_blank(raw)) {
            continue;
        }
        std::string text = collapse_whitespace(raw);
        if (codepoint_length(text) > width) {
            text = utf8::truncate(text, width > 0 ? width - 1 : 0) + kEllipsis;
        }
        return text;
    }

    std::string keys;
    std::size_t count = 0;
    for (auto it = payload.begin(); it != payload.end() && count < 3; ++it, ++count) {
        if (count > 0) {
            keys += ",";
        }
        keys += it.key();
    }
    return utf8::truncate(keys, width);
}

std::string ToolActivity::icon() const {
    if (status == "completed") {
        return kIconCompleted;
    }
    if (status == "error") {
        return kIconError;
    }
    if (status == "running") {
        return kIconRunning;
    }
    return kIconOther;
}

std::string ToolActivity::label() const {
    const std::string base = (name.empty() || name == kEllipsis) ? "tool" : name;
    if (!detail.empty()) {
        return base + "(" + detail + ")";
    }
    return base;
}

Step::Step(std::string id) : message_id(std::move(id)), started(monotonic_seconds()) {}

void Step::add_text(int ordinal, const std::string& delta) {
    if (segments.find(ordinal) == segments.end()) {
        segments[ordinal] = "";
        order.push_back(ordinal);
    }
    segments[ordinal] += delta;
}

void Step::add_reasoning(int ordinal, const std::string& delta) {
    if (reasoning.find(ordinal) == reasoning.end()) {
        reasoning[ordinal] = "";
        reasoning_order.push_back(ordinal);
    }
    reasoning[ordinal] += delta;
}

std::string Step::text() const {
    auto sorted = order;
    std::sort(sorted.begin(), sorted.end());
    std::string out;
    for (const int ordinal : sorted) {
        const auto it = segments.find(ordinal);
        if (it != segments.end()) {
            out += it->second;
        }
    }
    return out;
}

std::string Step::thinking() const {
    auto sorted = reasoning_order;
    std::sort(sorted.begin(), sorted.end());
    std::string out;
    for (const int ordinal : sorted) {
        const auto it = reasoning.find(ordinal);
        if (it != reasoning.end()) {
            out += it->second;
        }
    }
    return strip_copy(out);
}

bool Step::has_text() const { return !is_blank(text()); }

double Step::duration() const { return monotonic_seconds() - started; }

std::shared_ptr<ToolActivity> Step::tool(const std::string& call_id) const {
    for (const auto& [key, value] : tools) {
        if (key == call_id) {
            return value;
        }
    }
    return nullptr;
}

std::shared_ptr<ToolActivity> Step::tool_or_create(const std::string& call_id) {
    if (auto existing = tool(call_id)) {
        return existing;
    }
    auto created = std::make_shared<ToolActivity>();
    created->call_id = call_id;
    tools.emplace_back(call_id, created);
    return created;
}

TurnState::TurnState(std::string id) : session_id(std::move(id)), started(monotonic_seconds()) {}

double TurnState::duration() const { return monotonic_seconds() - started; }

std::string TurnState::text() const {
    std::string out;
    for (const auto& step : steps) {
        if (!step->has_text()) {
            continue;
        }
        if (!out.empty()) {
            out += "\n\n";
        }
        out += step->text();
    }
    return strip_copy(out);
}

std::shared_ptr<Step> TurnState::step_for(const std::string& message_id) const {
    for (const auto& step : steps) {
        if (step->message_id == message_id) {
            return step;
        }
    }
    return nullptr;
}

std::map<std::string, std::string> TurnState::tool_names() const {
    std::map<std::string, std::string> names;
    for (const auto& step : steps) {
        for (const auto& [call_id, tool] : step->tools) {
            if (!tool->name.empty() && tool->name != kEllipsis) {
                names[step->message_id + ":" + call_id] = tool->name;
            }
        }
    }
    return names;
}

void apply_event(TurnState& state, const Json& event) {
    const std::string kind = string_or_empty(lookup(event, "type"));
    const Json data = object_or_empty(lookup(event, "data"));

    if (kind == "session.step.started") {
        step_started(state, data);
    } else if (kind == "session.text.started") {
        const auto step = current_or_started(state, data);
        const int ordinal = static_cast<int>(int_or_zero(lookup(data, "ordinal")));
        if (step->segments.find(ordinal) == step->segments.end()) {
            step->segments[ordinal] = "";
        }
        if (std::find(step->order.begin(), step->order.end(), ordinal) == step->order.end()) {
            step->order.push_back(ordinal);
        }
    } else if (kind == "session.text.delta") {
        current_or_started(state, data)
            ->add_text(static_cast<int>(int_or_zero(lookup(data, "ordinal"))),
                       string_or_empty(lookup(data, "delta")));
    } else if (kind == "session.reasoning.started") {
        current_or_started(state, data)
            ->add_reasoning(static_cast<int>(int_or_zero(lookup(data, "ordinal"))), "");
    } else if (kind == "session.reasoning.delta") {
        current_or_started(state, data)
            ->add_reasoning(static_cast<int>(int_or_zero(lookup(data, "ordinal"))),
                            string_or_empty(lookup(data, "delta")));
    } else if (kind == "session.reasoning.ended") {
        const auto step = current_or_started(state, data);
        if (const auto* text = lookup(data, "text"); text != nullptr && truthy(*text)) {
            step->reasoning[static_cast<int>(int_or_zero(lookup(data, "ordinal")))] = py_str(*text);
        }
    } else if (kind == "session.retry.scheduled" || kind == "session.retry.started") {
        state.retrying = true;
    } else if (kind == "session.tool.called") {
        const auto step = current_or_started(state, data);
        const auto tool = step->tool_or_create(string_or_empty(lookup(data, "id")));
        tool->status = "running";
        tool->detail = tool_detail(object_or_empty(lookup(data, "input")));
    } else if (kind == "session.tool.success") {
        if (const auto tool = find_tool(state, data)) {
            tool->status = "completed";
            const std::string detail = tool_detail(object_or_empty(lookup(data, "input")));
            if (!detail.empty()) {
                tool->detail = detail;
            }
        }
    } else if (kind == "session.tool.failed") {
        if (const auto tool = find_tool(state, data)) {
            tool->status = "error";
            tool->error = step_error_text(lookup(data, "error"), 200);
        }
    } else if (kind == "session.step.ended") {
        auto step = resolve_step(state, data, current_or_started(state, data));
        step->finished = true;
        if (const auto* finish = lookup(data, "finish")) {
            step->finish = py_str(*finish);
        }
        step->cost = float_or_zero(lookup(data, "cost"));
        step->tokens = object_or_empty(lookup(data, "tokens"));
        state.cost = std::max(state.cost, step->cost);
        state.tokens = step->tokens;
    } else if (kind == "session.step.failed") {
        auto step = resolve_step(state, data, current_or_started(state, data));
        step->finished = true;
        step->error = step_error_text(lookup(data, "error"), 300);
    } else if (kind == "session.usage.updated") {
        if (const auto* cost = lookup(data, "cost"); cost != nullptr && truthy(*cost)) {
            state.cost = float_or_zero(cost);
        }
        if (const auto* tokens = lookup(data, "tokens"); tokens != nullptr && truthy(*tokens)) {
            state.tokens = *tokens;
        }
    } else if (kind == "permission.asked") {
        if (const auto* id = lookup(data, "id"); id != nullptr && truthy(*id)) {
            const std::string key = py_str(*id);
            bool replaced = false;
            for (auto& [existing, payload] : state.permissions) {
                if (existing == key) {
                    payload = data;
                    replaced = true;
                    break;
                }
            }
            if (!replaced) {
                state.permissions.emplace_back(key, data);
            }
        }
    } else if (kind == "permission.replied") {
        const std::string key = string_or_empty(lookup(data, "requestID"));
        for (auto it = state.permissions.begin(); it != state.permissions.end(); ++it) {
            if (it->first == key) {
                state.permissions.erase(it);
                break;
            }
        }
    } else if (kind == "session.execution.succeeded") {
        state.outcome = "succeeded";
        state.retrying = false;
    } else if (kind == "session.execution.failed" || kind == "session.execution.aborted" ||
               kind == "session.error") {
        state.outcome = "failed";
        state.error = execution_error_text(lookup(data, "error"), 300);
    } else if (kind == "session.interrupted" || kind == "session.execution.interrupted") {
        state.outcome = "interrupted";
        if (!state.error.has_value() || state.error->empty()) {
            state.error = "The session was interrupted.";
        }
    }
}

void hydrate_from_message(TurnState& state, const Json& message) {
    if (!truthy(message)) {
        return;
    }
    const auto step = state.step_for(string_or_empty(lookup(message, "id")));
    if (step == nullptr) {
        return;
    }
    const auto* content = lookup(message, "content");
    if (content == nullptr || !content->is_array()) {
        return;
    }

    for (const auto& part : *content) {
        const std::string kind = string_or_empty(lookup(part, "type"));
        if (kind == "reasoning") {
            const auto* text = lookup(part, "text");
            if (text != nullptr && truthy(*text) && step->thinking().empty()) {
                step->add_reasoning(0, py_str(*text));
            }
            continue;
        }
        if (kind != "tool") {
            continue;
        }

        const auto tool = step->tool_or_create(string_or_empty(lookup(part, "id")));
        if (const auto* name = lookup(part, "name"); name != nullptr && truthy(*name)) {
            tool->name = py_str(*name);
        }
        const Json part_state = object_or_empty(lookup(part, "state"));
        const std::string status = string_or_empty(lookup(part_state, "status"));
        if (is_known_status(status)) {
            tool->status = status;
        }
        if (tool->detail.empty()) {
            tool->detail = tool_detail(object_or_empty(lookup(part_state, "input")));
        }
        if (const auto* error = lookup(part_state, "error");
            error != nullptr && truthy(*error) && tool->error.empty()) {
            tool->error = utf8::truncate(py_str(*error), 200);
        }
    }
}

std::string render_status(const TurnState& state, const std::string& model_label) {
    if (state.retrying) {
        return "\U0001F501 " + text::join_nonempty({"provider hiccup \u2014 retrying", model_label});
    }

    const auto& step = state.current;
    if (step == nullptr) {
        return "\u23f3 " + text::join_nonempty({"thinking", model_label});
    }

    std::vector<std::string> running;
    for (const auto& [call_id, tool] : step->tools) {
        if (tool->status == "running") {
            running.push_back(tool->name != kEllipsis ? tool->name : "tool");
        }
    }
    if (!running.empty()) {
        const std::size_t take = std::min<std::size_t>(3, running.size());
        std::string names;
        for (std::size_t i = 0; i < take; ++i) {
            if (i > 0) {
                names += ", ";
            }
            names += running[i];
        }
        return "\u23f3 " + text::join_nonempty({"running " + names, model_label});
    }
    if (!step->thinking().empty() && !step->has_text()) {
        return "\U0001F9E0 " + text::join_nonempty({"thinking it through", model_label});
    }
    if (!step->has_text()) {
        return "\u23f3 " + text::join_nonempty({"thinking", model_label});
    }
    return "\U0001F4AD " + text::join_nonempty({"thinking", model_label});
}

std::string render_reasoning(const std::string& value, std::size_t limit) {
    const std::string body = collapse_whitespace(value);
    if (body.empty()) {
        return "";
    }
    const std::string trimmed = utf8::truncate(body, limit);
    const auto lines = utf8::split_lines(utf8::decode(trimmed));
    std::string quoted;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        if (i > 0) {
            quoted += "\n";
        }
        quoted += "> " + utf8::encode(lines[i]);
    }
    if (quoted.empty()) {
        quoted = "> " + trimmed;
    }
    return "\U0001F9E0 *thinking*\n" + quoted;
}

std::string render_tools(const Step& step, std::size_t limit) {
    if (step.tools.empty()) {
        return "";
    }

    std::string summary;
    for (const auto& [call_id, tool] : step.tools) {
        if (!summary.empty()) {
            summary += " ";
        }
        summary += tool->icon() + " `" + (tool->name != kEllipsis ? tool->name : "tool") + "`";
    }

    std::vector<std::shared_ptr<ToolActivity>> errors;
    for (const auto& [call_id, tool] : step.tools) {
        if (tool->status == "error") {
            errors.push_back(tool);
        }
    }
    if (!errors.empty()) {
        // The original slices to the first two errors, then drops those without
        // an error string, so an empty slot does not pull a later one forward.
        const std::size_t take = std::min<std::size_t>(2, errors.size());
        std::string detail;
        for (std::size_t i = 0; i < take; ++i) {
            if (errors[i]->error.empty()) {
                continue;
            }
            if (!detail.empty()) {
                detail += "; ";
            }
            detail += errors[i]->name + ": " + errors[i]->error;
        }
        if (!detail.empty()) {
            summary += "\n\u26a0\ufe0f " + text::clip(detail, 160);
        }
    }
    return text::clip(summary, limit);
}

std::string render_footer(const TurnState& state, const Step& step,
                          const std::string& model_label) {
    const double cost = step.cost != 0.0 ? step.cost : state.cost;
    const Json& tokens = truthy(step.tokens) ? step.tokens : state.tokens;

    std::vector<std::string> bits = {
        model_label,
        text::human_cost(std::optional<double>(cost)),
        text::format_tokens(token_total(tokens)),
        text::duration(step.duration()),
    };

    std::vector<std::string> kept;
    for (auto& bit : bits) {
        if (!bit.empty() && bit != "$0.00" && bit != "0 tokens") {
            kept.push_back(std::move(bit));
        }
    }
    return text::join_nonempty(kept);
}

}  // namespace engine
