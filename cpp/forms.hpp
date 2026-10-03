#pragma once

#include <string>
#include "json.hpp"

namespace engine {
// Convert a Discord text input into the typed answer expected by OpenCode.
Json parse_form_input(const Json& field, const std::string& input);
}
