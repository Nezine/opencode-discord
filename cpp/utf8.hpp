#pragma once

// UTF-8 helpers that mirror Python's str semantics.
//
// Discord's 1900/2000 character budget is counted in code points, but
// std::string is bytes, so every length or slice in the text layer has to go
// through here or long emoji-heavy model output gets cut mid-glyph.

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace utf8 {

inline constexpr char32_t REPLACEMENT = 0xFFFD;

// Decode UTF-8, substituting U+FFFD for each malformed byte, matching Python's
// bytes.decode("utf-8", "replace").
inline std::vector<char32_t> decode(std::string_view in) {
    std::vector<char32_t> out;
    out.reserve(in.size());

    std::size_t i = 0;
    const std::size_t n = in.size();
    while (i < n) {
        const auto b0 = static_cast<unsigned char>(in[i]);

        if (b0 < 0x80) {
            out.push_back(static_cast<char32_t>(b0));
            ++i;
            continue;
        }

        std::size_t len = 0;
        char32_t cp = 0;
        if ((b0 & 0xE0) == 0xC0) {
            len = 2;
            cp = b0 & 0x1F;
        } else if ((b0 & 0xF0) == 0xE0) {
            len = 3;
            cp = b0 & 0x0F;
        } else if ((b0 & 0xF8) == 0xF0) {
            len = 4;
            cp = b0 & 0x07;
        } else {
            out.push_back(REPLACEMENT);
            ++i;
            continue;
        }

        if (i + len > n) {
            out.push_back(REPLACEMENT);
            ++i;
            continue;
        }

        bool ok = true;
        for (std::size_t k = 1; k < len; ++k) {
            const auto bk = static_cast<unsigned char>(in[i + k]);
            if ((bk & 0xC0) != 0x80) {
                ok = false;
                break;
            }
            cp = static_cast<char32_t>((cp << 6) | (bk & 0x3F));
        }

        const bool overlong = (len == 2 && cp < 0x80) || (len == 3 && cp < 0x800) ||
                              (len == 4 && cp < 0x10000);
        const bool surrogate = cp >= 0xD800 && cp <= 0xDFFF;
        if (!ok || overlong || surrogate || cp > 0x10FFFF) {
            out.push_back(REPLACEMENT);
            ++i;
            continue;
        }

        out.push_back(cp);
        i += len;
    }
    return out;
}

inline std::string encode(const std::vector<char32_t>& cps) {
    std::string out;
    out.reserve(cps.size());
    for (const char32_t cp : cps) {
        if (cp < 0x80) {
            out.push_back(static_cast<char>(cp));
        } else if (cp < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else if (cp < 0x10000) {
            out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
    }
    return out;
}

// The exact set Python's str.isspace() (and regex \s on str) accepts. This is
// wider than C's isspace: it includes U+00A0, U+2000-U+200A, U+3000 and friends,
// which is what the fence regex sees in real model output.
inline bool is_space(char32_t c) {
    switch (c) {
        case 0x09:
        case 0x0A:
        case 0x0B:
        case 0x0C:
        case 0x0D:
        case 0x1C:
        case 0x1D:
        case 0x1E:
        case 0x1F:
        case 0x20:
        case 0x85:
        case 0xA0:
        case 0x1680:
        case 0x2028:
        case 0x2029:
        case 0x202F:
        case 0x205F:
        case 0x3000:
            return true;
        default:
            return c >= 0x2000 && c <= 0x200A;
    }
}

// The line boundaries Python's str.splitlines() recognises, minus \r, which the
// caller pairs with a following \n.
inline bool is_line_break(char32_t c) {
    switch (c) {
        case 0x0A:
        case 0x0B:
        case 0x0C:
        case 0x1C:
        case 0x1D:
        case 0x1E:
        case 0x85:
        case 0x2028:
        case 0x2029:
            return true;
        default:
            return false;
    }
}

// Python str.splitlines(): no trailing empty element when the text ends on a
// separator.
inline std::vector<std::vector<char32_t>> split_lines(const std::vector<char32_t>& text) {
    std::vector<std::vector<char32_t>> out;
    std::vector<char32_t> current;
    std::size_t i = 0;
    while (i < text.size()) {
        const char32_t c = text[i];
        if (c == U'\r') {
            out.push_back(current);
            current.clear();
            ++i;
            if (i < text.size() && text[i] == U'\n') {
                ++i;
            }
            continue;
        }
        if (is_line_break(c)) {
            out.push_back(current);
            current.clear();
            ++i;
            continue;
        }
        current.push_back(c);
        ++i;
    }
    if (!current.empty()) {
        out.push_back(current);
    }
    return out;
}

// Python's s[:limit] for str: cut on a code-point boundary, no ellipsis.
inline std::string truncate(std::string_view value, std::size_t limit) {
    auto cps = decode(value);
    if (cps.size() <= limit) {
        return std::string(value);
    }
    cps.resize(limit);
    return encode(cps);
}

inline void rstrip_in_place(std::vector<char32_t>& cps) {
    while (!cps.empty() && is_space(cps.back())) {
        cps.pop_back();
    }
}

inline void strip_in_place(std::vector<char32_t>& cps) {
    rstrip_in_place(cps);
    std::size_t start = 0;
    while (start < cps.size() && is_space(cps[start])) {
        ++start;
    }
    if (start > 0) {
        cps.erase(cps.begin(), cps.begin() + static_cast<std::ptrdiff_t>(start));
    }
}

}  // namespace utf8
