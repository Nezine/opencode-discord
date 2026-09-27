#pragma once

// Python dicts preserve insertion order; nlohmann's default object type sorts
// keys instead. Every payload that crosses the boundary is a Python dict, so the
// engine uses ordered_json throughout to keep iteration order identical.

#include <nlohmann/json.hpp>

namespace engine {

using Json = nlohmann::ordered_json;

}  // namespace engine
