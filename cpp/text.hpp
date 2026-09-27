#pragma once

// Text helpers for getting arbitrary model output into Discord messages.
//
// Port of bot/textutil.py. Discord hard-caps a message at 2000 characters and
// renders unbalanced code fences badly, so long answers are split on natural
// boundaries while keeping fences balanced.
//
// All functions take and return UTF-8 std::string; lengths are code points, not
// bytes, exactly as Python's len() would report them.

#include <cstddef>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace text {

inline constexpr std::size_t LIMIT = 1900;

// Truncate to `limit` code points, trimming trailing whitespace before the
// ellipsis. Returns input unchanged when it already fits.
std::string clip(std::string_view value, std::size_t limit = LIMIT);

// Split into Discord-sized chunks, re-balancing code fences inside each chunk.
// Whitespace-only chunks are dropped.
std::vector<std::string> split_text(std::string_view value, std::size_t limit = LIMIT);

// Neutralise @everyone / @here with a zero-width space. Second layer behind
// allowed_mentions=AllowedMentions.none() on every send path.
std::string sanitize_mentions(std::string_view value);

// `ts` is in milliseconds. Absent/zero renders "unknown".
std::string rel_time(std::optional<double> ts);
std::string rel_time(std::optional<double> ts, double now_seconds);

std::string duration(double seconds);

// Absent/zero renders "$0.00".
std::string human_cost(std::optional<double> cost);

// `total` is an already-summed token count.
std::string format_tokens(long long total);

}  // namespace text
