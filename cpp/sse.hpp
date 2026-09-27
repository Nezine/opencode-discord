#pragma once

// Incremental parser for the OpenCode server-sent-event stream.
//
// Port of bot/oc.py::SSEParser. aiohttp's line reader refuses any single line
// over 512 KiB and one OpenCode event can easily be larger than that (a tool
// that returns a whole file), so the raw byte stream is split here instead with
// a generous per-line cap: an event past the cap is dropped rather than being
// allowed to wedge the reader.

#include <cstddef>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

namespace engine {

class SseParser {
public:
    // Byte cap for a single line. Mutable to match the Python class attribute,
    // which tests lower to exercise the drop path.
    std::size_t max_line = 8 * 1024 * 1024;

    // Diagnostics, exposed read-only to Python.
    std::size_t dropped = 0;
    std::size_t largest = 0;  // code points of the largest data payload seen

    // Add bytes and return every event completed by them.
    std::vector<nlohmann::json> feed(std::string_view chunk);

private:
    std::optional<nlohmann::json> parse_line(std::string_view raw);

    std::string buffer_;
    std::optional<std::string> name_;
};

}  // namespace engine
