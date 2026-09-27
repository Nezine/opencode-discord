#include "sse.hpp"

#include <algorithm>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "utf8.hpp"

namespace engine {
namespace {

// Python: raw.decode("utf-8", "replace").rstrip("\r")
std::string decode_and_trim_cr(std::string_view raw) {
    std::string line = utf8::encode(utf8::decode(raw));
    while (!line.empty() && line.back() == '\r') {
        line.pop_back();
    }
    return line;
}

bool starts_with(std::string_view value, std::string_view prefix) {
    return value.size() >= prefix.size() && value.compare(0, prefix.size(), prefix) == 0;
}

// Python str.strip() over the Unicode whitespace set.
std::string strip(std::string_view value) {
    auto cps = utf8::decode(value);
    utf8::strip_in_place(cps);
    return utf8::encode(cps);
}

}  // namespace

std::vector<Json> SseParser::feed(std::string_view chunk) {
    buffer_.append(chunk);
    std::vector<Json> events;

    while (true) {
        const std::size_t index = buffer_.find('\n');
        if (index == std::string::npos) {
            // A line that never terminates still must not grow without bound.
            if (buffer_.size() > max_line) {
                dropped += 1;
                buffer_.clear();
            }
            break;
        }

        std::string line = buffer_.substr(0, index);
        buffer_.erase(0, index + 1);

        if (line.size() > max_line) {
            dropped += 1;
            continue;
        }

        auto event = parse_line(line);
        if (event.has_value()) {
            events.push_back(std::move(*event));
        }
    }
    return events;
}

std::optional<Json> SseParser::parse_line(std::string_view raw) {
    const std::string line = decode_and_trim_cr(raw);
    if (line.empty()) {
        return std::nullopt;
    }

    if (starts_with(line, "event:")) {
        name_ = strip(std::string_view(line).substr(6));
        return std::nullopt;
    }
    if (!starts_with(line, "data:")) {
        return std::nullopt;
    }

    const std::string data = strip(std::string_view(line).substr(5));
    if (data.empty() || data == "[DONE]") {
        return std::nullopt;
    }

    largest = std::max(largest, utf8::decode(data).size());

    Json event;
    try {
        event = Json::parse(data);
    } catch (const Json::exception&) {
        return std::nullopt;
    }
    if (!event.is_object()) {
        return std::nullopt;
    }

    // An empty `event:` name is falsy in Python, so it is never injected.
    if (name_.has_value() && !name_->empty() && !event.contains("type")) {
        event["type"] = *name_;
    }
    name_.reset();
    return event;
}

}  // namespace engine
