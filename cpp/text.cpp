#include "text.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <ctime>
#include <string>
#include <utility>
#include <vector>

#include "utf8.hpp"

namespace text {
namespace {

using Cps = std::vector<char32_t>;
using Block = std::pair<std::string, std::vector<Cps>>;

double now_seconds() {
    const auto now = std::chrono::system_clock::now().time_since_epoch();
    return std::chrono::duration<double>(now).count();
}

// FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
//
// Returns true and fills `marker` when the line opens a fence, so the caller can
// compare marker type (backtick vs tilde) and length against a candidate close.
bool fence_marker(const Cps& line, Cps& marker) {
    std::size_t i = 0;
    while (i < line.size() && utf8::is_space(line[i])) {
        ++i;
    }
    if (i >= line.size()) {
        return false;
    }
    const char32_t ch = line[i];
    if (ch != U'`' && ch != U'~') {
        return false;
    }
    std::size_t j = i;
    while (j < line.size() && line[j] == ch) {
        ++j;
    }
    if (j - i < 3) {
        return false;
    }
    marker.assign(line.begin() + static_cast<std::ptrdiff_t>(i),
                  line.begin() + static_cast<std::ptrdiff_t>(j));
    return true;
}

std::string join_lines(const std::vector<Cps>& lines) {
    std::string out;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        if (i > 0) {
            out.push_back('\n');
        }
        out += utf8::encode(lines[i]);
    }
    return out;
}

// Split into ("prose"|"code", lines) blocks. A code block runs from its opening
// fence to the matching close, so blocks can be re-packed without ever
// producing an unbalanced message.
std::vector<Block> split_blocks(const Cps& text) {
    const auto lines = utf8::split_lines(text);
    std::vector<Block> out;
    std::vector<Cps> prose;
    std::size_t index = 0;

    while (index < lines.size()) {
        Cps marker;
        if (!fence_marker(lines[index], marker)) {
            prose.push_back(lines[index]);
            ++index;
            continue;
        }
        if (!prose.empty()) {
            out.emplace_back("prose", std::move(prose));
            prose.clear();
        }
        auto code = std::vector<Cps>{lines[index]};
        ++index;
        while (index < lines.size()) {
            code.push_back(lines[index]);
            Cps inner;
            if (fence_marker(lines[index], inner) && inner[0] == marker[0] &&
                inner.size() >= marker.size()) {
                ++index;
                break;
            }
            ++index;
        }
        out.emplace_back("code", std::move(code));
    }
    if (!prose.empty()) {
        out.emplace_back("prose", std::move(prose));
    }
    return out;
}

// Break one fenced region into fence-balanced chunks of at most `limit` code
// points. Note `info` is sliced from index 0 by marker *length*, not from the
// marker's match offset, and the original does the same -- indented fences
// duplicate part of the marker. Kept bug-compatible on purpose.
std::vector<std::string> split_code(const Cps& marker, const std::vector<Cps>& lines,
                                    std::size_t limit) {
    Cps open_line = lines.front();
    Cps info(open_line.begin() + static_cast<std::ptrdiff_t>(marker.size()), open_line.end());
    std::vector<Cps> body(lines.begin() + 1, lines.end());

    bool closed = false;
    if (!body.empty()) {
        Cps tail;
        if (fence_marker(body.back(), tail) && tail[0] == marker[0]) {
            body.pop_back();
            closed = true;
        }
    }

    open_line = marker;
    open_line.insert(open_line.end(), info.begin(), info.end());

    const auto raw_budget = static_cast<long long>(limit) -
                            static_cast<long long>(open_line.size()) -
                            static_cast<long long>(marker.size()) - 2;
    const std::size_t budget = raw_budget < 20 ? 20 : static_cast<std::size_t>(raw_budget);

    std::vector<std::string> chunks;
    std::vector<Cps> piece;
    std::size_t size = 0;

    const auto emit = [&]() {
        std::vector<Cps> parts;
        parts.reserve(piece.size() + 2);
        parts.push_back(open_line);
        parts.insert(parts.end(), piece.begin(), piece.end());
        parts.push_back(marker);
        chunks.push_back(join_lines(parts));
        piece.clear();
        size = 0;
    };

    for (Cps line : body) {
        while (line.size() > budget) {
            if (!piece.empty()) {
                emit();
            }
            piece.emplace_back(line.begin(), line.begin() + static_cast<std::ptrdiff_t>(budget));
            line.erase(line.begin(), line.begin() + static_cast<std::ptrdiff_t>(budget));
            emit();
        }
        if (!piece.empty() && size + line.size() + 1 > budget) {
            emit();
        }
        piece.push_back(std::move(line));
        size += piece.back().size() + 1;
    }
    if (!piece.empty() || !closed) {
        emit();
    }
    return chunks;
}

void replace_all(std::string& haystack, std::string_view needle, std::string_view replacement) {
    std::size_t pos = 0;
    while ((pos = haystack.find(needle, pos)) != std::string::npos) {
        haystack.replace(pos, needle.size(), replacement);
        pos += replacement.size();
    }
}

}  // namespace

std::string clip(std::string_view value, std::size_t limit) {
    if (value.empty()) {
        return "";
    }
    Cps cps = utf8::decode(value);
    if (cps.size() <= limit) {
        return std::string(value);
    }
    const std::size_t keep = limit > 3 ? limit - 3 : 0;
    cps.resize(std::min(keep, cps.size()));
    utf8::rstrip_in_place(cps);
    return utf8::encode(cps) + "...";
}

std::vector<std::string> split_text(std::string_view value, std::size_t limit) {
    if (value.empty()) {
        return {};
    }
    const Cps cps = utf8::decode(value);
    if (cps.size() <= limit) {
        return {std::string(value)};
    }

    std::vector<std::string> out;
    std::vector<Cps> current;
    std::size_t size = 0;

    const auto flush = [&]() {
        if (!current.empty()) {
            out.push_back(join_lines(current));
            current.clear();
            size = 0;
        }
    };

    for (const auto& block : split_blocks(cps)) {
        if (block.first == "code") {
            flush();
            Cps marker;
            fence_marker(block.second.front(), marker);
            for (auto& chunk : split_code(marker, block.second, limit)) {
                out.push_back(std::move(chunk));
            }
            continue;
        }
        for (Cps line : block.second) {
            if (size + line.size() + 1 > limit && !current.empty()) {
                flush();
            }
            while (line.size() > limit) {
                flush();
                out.push_back(utf8::encode(Cps(line.begin(), line.begin() +
                                                              static_cast<std::ptrdiff_t>(limit))));
                line.erase(line.begin(), line.begin() + static_cast<std::ptrdiff_t>(limit));
            }
            current.push_back(std::move(line));
            size += current.back().size() + 1;
        }
    }
    flush();

    std::vector<std::string> result;
    result.reserve(out.size());
    for (auto& chunk : out) {
        Cps stripped = utf8::decode(chunk);
        utf8::strip_in_place(stripped);
        if (!stripped.empty()) {
            result.push_back(std::move(chunk));
        }
    }
    return result;
}

std::string sanitize_mentions(std::string_view value) {
    if (value.empty()) {
        return "";
    }
    const std::string zero_width = "\u200b";
    std::string out(value);
    replace_all(out, "@everyone", "@" + zero_width + "everyone");
    replace_all(out, "@here", "@" + zero_width + "here");
    return out;
}

std::string rel_time(std::optional<double> ts) { return rel_time(ts, now_seconds()); }

std::string rel_time(std::optional<double> ts, double now) {
    if (!ts.has_value() || *ts == 0.0) {
        return "unknown";
    }
    double delta = now - *ts / 1000.0;
    if (delta < 0.0) {
        delta = 0.0;
    }
    if (delta < 60) {
        return "just now";
    }
    if (delta < 3600) {
        return std::to_string(static_cast<long long>(delta / 60)) + "m ago";
    }
    if (delta < 86400) {
        return std::to_string(static_cast<long long>(delta / 3600)) + "h ago";
    }
    if (delta < 86400 * 30) {
        return std::to_string(static_cast<long long>(delta / 86400)) + "d ago";
    }

    const auto seconds = static_cast<std::time_t>(*ts / 1000.0);
    std::tm local{};
    localtime_r(&seconds, &local);
    char buf[16] = {};
    std::strftime(buf, sizeof buf, "%Y-%m-%d", &local);
    return buf;
}

std::string duration(double seconds) {
    char buf[64] = {};
    if (seconds < 1) {
        std::snprintf(buf, sizeof buf, "%.0fms", seconds * 1000.0);
        return buf;
    }
    if (seconds < 60) {
        std::snprintf(buf, sizeof buf, "%.1fs", seconds);
        return buf;
    }
    const auto total = static_cast<long long>(seconds);
    std::snprintf(buf, sizeof buf, "%lldm %llds", total / 60, total % 60);
    return buf;
}

std::string human_cost(std::optional<double> cost) {
    if (!cost.has_value() || *cost == 0.0) {
        return "$0.00";
    }
    char buf[64] = {};
    if (*cost < 0.01) {
        std::snprintf(buf, sizeof buf, "$%.4f", *cost);
        return buf;
    }
    std::snprintf(buf, sizeof buf, "$%.2f", *cost);
    return buf;
}

std::string format_tokens(long long total) {
    char buf[64] = {};
    if (total >= 1000000) {
        std::snprintf(buf, sizeof buf, "%.1fM tokens", static_cast<double>(total) / 1e6);
        return buf;
    }
    if (total >= 1000) {
        std::snprintf(buf, sizeof buf, "%.1fk tokens", static_cast<double>(total) / 1e3);
        return buf;
    }
    return std::to_string(total) + " tokens";
}

std::string join_nonempty(const std::vector<std::string>& parts, std::string_view sep) {
    std::string out;
    for (const auto& part : parts) {
        if (part.empty()) {
            continue;
        }
        if (!out.empty()) {
            out += sep;
        }
        out += part;
    }
    return out;
}

}  // namespace text
