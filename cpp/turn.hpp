#pragma once

// Turn state machine: turns OpenCode events into renderable state.
//
// Port of bot/turn.py. Knows nothing about Discord: it consumes the event stream
// for one execution and exposes text plus tool activity, and the render_* helpers
// turn that state into message text.
//
// Pure C++ over Json so it can be exercised from engine_tests as well
// as through the extension module.

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "json.hpp"

namespace engine {

// A one-line hint of what a tool was called with.
std::string tool_detail(const Json& payload, std::size_t width = 42);

// Sum of input/output/reasoning plus cache.read/cache.write, matching how
// human_tokens() totalled them. Non-numeric entries are skipped the way Python's
// int(x or 0) swallows TypeError/ValueError.
long long token_total(const Json& tokens);

struct ToolActivity {
    std::string call_id;
    std::string name = "\u2026";
    std::string status = "running";
    std::string detail;
    std::string error;

    std::string icon() const;
    std::string label() const;
};

class Step {
public:
    explicit Step(std::string message_id);

    std::string message_id;
    // Segments are keyed by ordinal; text() joins them in ordinal order.
    std::map<int, std::string> segments;
    std::vector<int> order;
    std::map<int, std::string> reasoning;
    std::vector<int> reasoning_order;
    // Insertion-ordered, because render_tools() shows them in call order.
    std::vector<std::pair<std::string, std::shared_ptr<ToolActivity>>> tools;
    bool finished = false;
    std::optional<std::string> finish;
    std::optional<std::string> error;
    double cost = 0.0;
    Json tokens = Json::object();
    double started = 0.0;

    void add_text(int ordinal, const std::string& delta);
    void add_reasoning(int ordinal, const std::string& delta);

    std::string text() const;
    std::string thinking() const;
    bool has_text() const;
    double duration() const;

    // Lookup only, may return null.
    std::shared_ptr<ToolActivity> tool(const std::string& call_id) const;
    // Lookup, creating and recording a placeholder when absent.
    std::shared_ptr<ToolActivity> tool_or_create(const std::string& call_id);
};

// One option a form field offers the user.
struct FormOption {
    std::string value;
    std::string label;
    std::string description;
};

// One field in a form. Only the shapes the Discord surface can answer are
// modelled: option lists (single and multi select) and booleans. Text/number
// entry and external fields are carried as raw JSON so the bot can still show
// them, but they are not interactive.
struct FormField {
    std::string key;
    std::string type;
    std::string title;
    std::string description;
    bool required = false;
    std::vector<FormOption> options;
    Json raw = Json::object();

    bool has_options() const { return !options.empty(); }
};

// A pending "choose an option" prompt from OpenCode. Mirrors Form.Info.
struct Form {
    std::string id;
    std::string session_id;
    std::string title;
    std::vector<FormField> fields;
    Json raw = Json::object();

    const FormField* field(const std::string& key) const;
};

class TurnState {
public:
    explicit TurnState(std::string session_id);

    std::string session_id;
    double started = 0.0;
    std::vector<std::shared_ptr<Step>> steps;
    std::shared_ptr<Step> current;
    double cost = 0.0;
    Json tokens = Json::object();
    std::optional<std::string> outcome;
    std::optional<std::string> error;
    bool retrying = false;
    // Answered/unanswered permission requests, keyed by request id, in arrival
    // order. Only the count is consumed by the bot.
    std::vector<std::pair<std::string, Json>> permissions;
    // Active "choose an option" forms, keyed by form id, in arrival order.
    std::vector<std::pair<std::string, Form>> forms;

    double duration() const;
    std::string text() const;

    // Permission requests that have not been answered yet.
    std::size_t pending_permissions() const { return permissions.size(); }

    // Forms that have not been answered or cancelled yet.
    std::size_t pending_forms() const { return forms.size(); }
    const Form* form(const std::string& id) const;

    std::shared_ptr<Step> step_for(const std::string& message_id) const;
    std::map<std::string, std::string> tool_names() const;
};

// Fold one OpenCode event into the turn state.
void apply_event(TurnState& state, const Json& event);

// Fill in authoritative tool names/statuses from a fetched message.
void hydrate_from_message(TurnState& state, const Json& message);

std::string render_status(const TurnState& state, const std::string& model_label = "");
std::string render_reasoning(const std::string& value, std::size_t limit = 700);
std::string render_tools(const Step& step, std::size_t limit = 300);
std::string render_footer(const TurnState& state, const Step& step,
                          const std::string& model_label = "");

}  // namespace engine
