#include "forms.hpp"

#include <charconv>
#include <cmath>
#include <stdexcept>
#include "utf8.hpp"

namespace engine {
Json parse_form_input(const Json& field, const std::string& input) {
    auto chars = utf8::decode(input);
    utf8::strip_in_place(chars);
    const std::string trimmed = utf8::encode(chars);
    const std::string type = field.value("type", std::string("string"));
    if (trimmed.empty()) {
        if (field.value("required", true)) {
            throw std::invalid_argument("Please enter an answer.");
        }
        return type == "number" || type == "integer" ? Json(nullptr) : Json(input);
    }
    if (type == "string" || type == "text") {
        return input;  // Keep indentation and line breaks in code answers.
    }
    if (type == "integer") {
        long long value = 0;
        const auto [end, error] = std::from_chars(
            trimmed.data(), trimmed.data() + trimmed.size(), value);
        if (error != std::errc{} || end != trimmed.data() + trimmed.size()) {
            throw std::invalid_argument("Enter a whole number, for example 12.");
        }
        return value;
    }
    if (type == "number") {
        double value = 0;
        const auto [end, error] = std::from_chars(
            trimmed.data(), trimmed.data() + trimmed.size(), value);
        if (error != std::errc{} || end != trimmed.data() + trimmed.size() ||
            !std::isfinite(value)) {
            throw std::invalid_argument("Enter a finite number, for example 12.5.");
        }
        return value;
    }
    throw std::invalid_argument("This field does not accept text input.");
}
}
