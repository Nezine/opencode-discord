#include "workflow.hpp"
#include <algorithm>
#include <cctype>
#include <limits>
#include <stdexcept>
#include "utf8.hpp"

namespace engine {
namespace {
std::string string_field(const Json& value, const char* key) {
    const auto it = value.find(key);
    return it != value.end() && it->is_string() ? it->get<std::string>() : "";
}
void validate_prompt(const Json& payload) {
    if (!payload.is_object() || string_field(payload, "session_id").empty())
        throw std::invalid_argument("A queued message needs a conversation.");
    if (payload.contains("text") && !payload["text"].is_string())
        throw std::invalid_argument("Message text must be a string.");
    const auto text = string_field(payload, "text");
    if (text.size() > 128000) throw std::invalid_argument("Queued message is too long.");
    const auto files = payload.value("files", Json::array());
    if (!files.is_array() || files.size() > 10) throw std::invalid_argument("Too many attachments.");
    auto chars = utf8::decode(text);
    utf8::strip_in_place(chars);
    if (chars.empty() && files.empty()) throw std::invalid_argument("Enter a message or attach a file.");
}
long long queue_id(const Json& value) {
    if (!value.is_number_integer() || (value.is_number_unsigned() &&
            value.get<unsigned long long>() > static_cast<unsigned long long>(std::numeric_limits<long long>::max())))
        throw std::invalid_argument("Invalid saved queue ID.");
    return value.get<long long>();
}
Json prompt_fields(const Json& payload) {
    return Json{{"session_id", payload["session_id"]}, {"text", string_field(payload, "text")},
                {"files", payload.value("files", Json::array())}};
}
}
long long PromptQueue::push(const Json& payload) {
    if (items_.size() >= 20) throw std::invalid_argument("Queue is full (20 messages). Remove an item first.");
    validate_prompt(payload);
    if (next_id_ == std::numeric_limits<long long>::max()) throw std::overflow_error("Queue ID exhausted.");
    Json item = prompt_fields(payload);
    item["id"] = next_id_++;
    items_.push_back(item);
    return item["id"].get<long long>();
}
Json PromptQueue::pop() {
    if (items_.empty()) return nullptr;
    Json item = items_.front();
    items_.pop_front();
    return item;
}
bool PromptQueue::remove(long long id) {
    const auto it = std::find_if(items_.begin(), items_.end(), [&](const Json& item) { return item["id"] == id; });
    if (it == items_.end()) return false;
    items_.erase(it);
    return true;
}
void PromptQueue::clear() { items_.clear(); }
Json PromptQueue::entries() const { return Json(items_); }
Json PromptQueue::snapshot() const { return Json{{"next_id", next_id_}, {"items", entries()}}; }
void PromptQueue::restore(const Json& saved) {
    if (saved.is_null()) return;
    const auto& items = saved.at("items");
    const auto next = queue_id(saved.at("next_id"));
    if (!items.is_array() || items.size() > 20 || next < 1) throw std::invalid_argument("Invalid saved queue.");
    long long previous = 0;
    std::deque<Json> validated;
    for (const auto& item : items) {
        validate_prompt(item);
        const auto id = queue_id(item.at("id"));
        if (id <= previous || id >= next) throw std::invalid_argument("Invalid saved queue IDs.");
        previous = id;
        Json normalized = prompt_fields(item);
        normalized["id"] = id;
        validated.push_back(std::move(normalized));
    }
    items_ = std::move(validated);
    next_id_ = next;
}
std::string project_name(const std::string& name) {
    if (name.empty() || name.size() > 40) throw std::invalid_argument("Use a project name of 1–40 letters, digits, - or _.");
    std::string result;
    for (unsigned char c : name) {
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_'))
            throw std::invalid_argument("Use letters, digits, - or _ in the project name.");
        result += static_cast<char>(std::tolower(c));
    }
    return result;
}
Json transcript_entries(const Json& messages) {
    Json entries = Json::array();
    if (!messages.is_array()) throw std::invalid_argument("Messages must be a list.");
    for (const auto& message : messages) {
        if (!message.is_object()) continue;
        const auto role = string_field(message, "type");
        if (role != "user" && role != "assistant") continue;
        std::string text = role == "user" ? string_field(message, "text") : "";
        if (role == "assistant" && message.contains("content") && message["content"].is_array()) {
            for (const auto& part : message["content"])
                if (part.is_object() && string_field(part, "type") == "text") text += string_field(part, "text");
        }
        if (!text.empty()) entries.push_back(Json{{"id", string_field(message, "id")}, {"role", role}, {"text", text}});
    }
    return entries;
}
std::string export_transcript(const Json& session, const Json& messages, const std::string& format) {
    const Json entries = transcript_entries(messages);
    if (format == "json")
        return Json{{"session_id", string_field(session, "id")}, {"title", string_field(session, "title")},
                    {"messages", entries}}.dump(2);
    if (format != "markdown") throw std::invalid_argument("Choose markdown or json.");
    auto title = string_field(session, "title");
    std::replace(title.begin(), title.end(), '\n', ' ');
    std::replace(title.begin(), title.end(), '\r', ' ');
    std::string out = "# " + (title.empty() ? std::string("Conversation") : title) + "\n\n";
    for (const auto& entry : entries)
        out += std::string("## ") + (entry["role"] == "user" ? "You" : "Assistant") + "\n\n" + entry["text"].get<std::string>() + "\n\n";
    return out;
}
}
