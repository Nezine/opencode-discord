#pragma once
#include <deque>
#include "json.hpp"

namespace engine {
// Event-loop owned; entries contain data, never Discord/Python objects.
class PromptQueue {
public:
    long long push(const Json& payload);
    Json pop();
    bool remove(long long id);
    void clear();
    Json entries() const;
    Json snapshot() const;
    void restore(const Json& saved);
    std::size_t size() const { return items_.size(); }
private:
    std::deque<Json> items_;
    long long next_id_ = 1;
};
std::string project_name(const std::string& name);
Json transcript_entries(const Json& messages);
std::string export_transcript(const Json& session, const Json& messages, const std::string& format);
}
