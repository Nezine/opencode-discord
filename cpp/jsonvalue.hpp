#pragma once

// Python-truthiness and str() for the JSON subset these payloads use. Shared by
// the turn machine and the transport, which both mirror Python's `or` defaults
// and error formatting.

#include <string>

#include "json.hpp"

namespace engine {

inline bool truthy(const Json& value) {
    switch (value.type()) {
        case Json::value_t::null:
            return false;
        case Json::value_t::boolean:
            return value.get<bool>();
        case Json::value_t::number_integer:
            return value.get<long long>() != 0;
        case Json::value_t::number_unsigned:
            return value.get<unsigned long long>() != 0;
        case Json::value_t::number_float:
            return value.get<double>() != 0.0;
        case Json::value_t::string:
            return !value.get_ref<const std::string&>().empty();
        case Json::value_t::array:
            return !value.empty();
        case Json::value_t::object:
            return !value.empty();
        default:
            return false;
    }
}

// str(value) for the scalar shapes these payloads carry. Note that a container
// renders as JSON here where Python would use repr().
inline std::string py_str(const Json& value) {
    if (value.is_string()) {
        return value.get<std::string>();
    }
    if (value.is_null()) {
        return "None";
    }
    if (value.is_boolean()) {
        return value.get<bool>() ? "True" : "False";
    }
    return value.dump();
}

}  // namespace engine
