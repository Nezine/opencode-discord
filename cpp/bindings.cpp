#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstddef>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "sse.hpp"
#include "store.hpp"
#include "text.hpp"

namespace py = pybind11;

namespace {

// nlohmann::json -> plain Python objects. Callers poke these with dict.get(),
// isinstance() and truthiness, so they must be real dict/list/scalars and not
// wrapper types.
py::object json_to_py(const nlohmann::json& value) {
    using value_t = nlohmann::json::value_t;
    switch (value.type()) {
        case value_t::null:
            return py::none();
        case value_t::boolean:
            return py::bool_(value.get<bool>());
        case value_t::number_integer:
            return py::int_(value.get<long long>());
        case value_t::number_unsigned:
            return py::int_(value.get<unsigned long long>());
        case value_t::number_float:
            return py::float_(value.get<double>());
        case value_t::string:
            return py::str(value.get_ref<const std::string&>());
        case value_t::array: {
            py::list out;
            for (const auto& item : value) {
                out.append(json_to_py(item));
            }
            return out;
        }
        case value_t::object: {
            py::dict out;
            for (auto it = value.begin(); it != value.end(); ++it) {
                out[py::str(it.key())] = json_to_py(it.value());
            }
            return out;
        }
        default:
            return py::none();
    }
}

// The Python helpers are all written as `text or ""`, and the message layer
// really does pass None when a step produced no content, so falsy input has to
// collapse to the empty string rather than raise.
std::string as_text(py::handle value) {
    if (!value || PyObject_IsTrue(value.ptr()) != 1) {
        return "";
    }
    return py::cast<std::string>(value);
}

// `mapping.get(key)` when the object has one, else None -- mirrors how the
// Python helpers probe these payloads.
py::object mapping_get(py::handle mapping, const char* key) {
    if (!mapping || !py::hasattr(mapping, "get")) {
        return py::none();
    }
    try {
        return mapping.attr("get")(py::str(key));
    } catch (const py::error_already_set&) {
        return py::none();
    }
}

// Python's `int(value or 0)` wrapped in `except (TypeError, ValueError): pass`.
void add_token_count(py::handle mapping, const char* key, long long& total) {
    const py::object value = mapping_get(mapping, key);
    if (!value || PyObject_IsTrue(value.ptr()) != 1) {
        return;
    }
    try {
        const py::object coerced = py::reinterpret_steal<py::object>(PyNumber_Long(value.ptr()));
        if (coerced) {
            total += py::cast<long long>(coerced);
        }
    } catch (const py::error_already_set&) {
        PyErr_Clear();
    }
}

}  // namespace

PYBIND11_MODULE(_engine, m) {
    m.doc() =
        "Native engine for opencode-discord.\n\n"
        "Owns the OpenCode side of the bridge: HTTP/SSE transport, SSE framing, "
        "the turn state machine, text chunking, and SQLite persistence.\n"
        "The Discord-facing layer stays in Python.";
    m.attr("__version__") = "0.1.0";

    m.def("hello", []() { return std::string("engine online"); });

    // ------------------------------------------------------------------ text

    m.attr("LIMIT") = text::LIMIT;

    m.def(
        "clip",
        [](py::object value, std::size_t limit) { return text::clip(as_text(value), limit); },
        py::arg("text"), py::arg("limit") = text::LIMIT);

    m.def(
        "split_text",
        [](py::object value, std::size_t limit) {
            return text::split_text(as_text(value), limit);
        },
        py::arg("text"), py::arg("limit") = text::LIMIT);

    m.def("sanitize_mentions",
          [](py::object value) { return text::sanitize_mentions(as_text(value)); },
          py::arg("text"));

    m.def(
        "rel_time",
        [](py::object ts) -> std::string {
            if (!ts || PyObject_IsTrue(ts.ptr()) != 1) {
                return text::rel_time(std::nullopt);
            }
            return text::rel_time(py::cast<double>(ts));
        },
        py::arg("ts"));

    m.def("duration", [](double seconds) { return text::duration(seconds); }, py::arg("seconds"));

    m.def(
        "human_cost",
        [](py::object cost) -> std::string {
            if (!cost || PyObject_IsTrue(cost.ptr()) != 1) {
                return text::human_cost(std::nullopt);
            }
            return text::human_cost(py::cast<double>(cost));
        },
        py::arg("cost"));

    m.def(
        "human_tokens",
        [](py::object tokens) -> std::string {
            if (!tokens || PyObject_IsTrue(tokens.ptr()) != 1) {
                return text::format_tokens(0);
            }
            long long total = 0;
            for (const char* key : {"input", "output", "reasoning"}) {
                add_token_count(tokens, key, total);
            }
            const py::object cache = mapping_get(tokens, "cache");
            if (cache && PyObject_IsTrue(cache.ptr()) == 1) {
                for (const char* key : {"read", "write"}) {
                    add_token_count(cache, key, total);
                }
            }
            return text::format_tokens(total);
        },
        py::arg("tokens"));

    m.def(
        "join_nonempty",
        [](py::iterable parts, std::string sep) {
            std::string out;
            bool first = true;
            for (py::handle part : parts) {
                if (!part || PyObject_IsTrue(part.ptr()) != 1) {
                    continue;
                }
                if (!first) {
                    out += sep;
                }
                out += py::cast<std::string>(part);
                first = false;
            }
            return out;
        },
        py::arg("parts"), py::arg("sep") = " \u00b7 ");

    // -------------------------------------------------------------- SSE parser

    py::class_<engine::SseParser>(m, "SSEParser",
                                  "Incremental parser for the OpenCode server-sent-event "
                                  "stream. Splits the raw byte stream so a single event "
                                  "larger than aiohttp's 512 KiB line cap still parses, and "
                                  "drops an event past the cap instead of wedging the reader.")
        .def(py::init<>())
        .def_property(
            "MAX_LINE", [](const engine::SseParser& self) { return self.max_line; },
            [](engine::SseParser& self, std::size_t value) { self.max_line = value; })
        .def_readonly("dropped", &engine::SseParser::dropped)
        .def_readonly("largest", &engine::SseParser::largest)
        .def(
            "feed",
            [](engine::SseParser& self, const std::string& chunk) {
                std::vector<nlohmann::json> events = self.feed(chunk);
                py::list out(events.size());
                for (std::size_t i = 0; i < events.size(); ++i) {
                    out[i] = json_to_py(events[i]);
                }
                return out;
            },
            py::arg("chunk"), "Add bytes and return every event completed by them.");

    // ----------------------------------------------------------------- store

    py::class_<engine::UserState>(m, "UserState",
                                  "Per-user chat state: the active conversation plus the "
                                  "model / effort / agent preferences.")
        .def(py::init([](long long user_id, std::optional<std::string> session_id,
                         std::optional<std::string> provider_id, std::optional<std::string> model_id,
                         std::optional<std::string> variant, std::optional<std::string> agent,
                         std::optional<std::string> directory, std::optional<std::string> title) {
                 engine::UserState state;
                 state.user_id = user_id;
                 state.session_id = std::move(session_id);
                 state.provider_id = std::move(provider_id);
                 state.model_id = std::move(model_id);
                 state.variant = std::move(variant);
                 state.agent = std::move(agent);
                 state.directory = std::move(directory);
                 state.title = std::move(title);
                 return state;
             }),
             py::arg("user_id"), py::arg("session_id") = std::nullopt,
             py::arg("provider_id") = std::nullopt, py::arg("model_id") = std::nullopt,
             py::arg("variant") = std::nullopt, py::arg("agent") = std::nullopt,
             py::arg("directory") = std::nullopt, py::arg("title") = std::nullopt)
        .def_readwrite("user_id", &engine::UserState::user_id)
        .def_readwrite("session_id", &engine::UserState::session_id)
        .def_readwrite("provider_id", &engine::UserState::provider_id)
        .def_readwrite("model_id", &engine::UserState::model_id)
        .def_readwrite("variant", &engine::UserState::variant)
        .def_readwrite("agent", &engine::UserState::agent)
        .def_readwrite("directory", &engine::UserState::directory)
        .def_readwrite("title", &engine::UserState::title)
        .def_property_readonly("has_model", &engine::UserState::has_model)
        .def_property_readonly("model_label", &engine::UserState::model_label)
        .def("__repr__", [](const engine::UserState& self) {
            return "UserState(user_id=" + std::to_string(self.user_id) + ", model=" +
                   self.model_label() + ")";
        });

    py::class_<engine::Store>(m, "Store", "SQLite persistence for per-user chat state.")
        .def(
            py::init([](py::object path) {
                // The original accepted str | Path and called str() on it.
                return std::make_unique<engine::Store>(py::cast<std::string>(py::str(path)));
            }),
            py::arg("path"))
        .def_property_readonly("path", &engine::Store::path)
        .def("get_user", &engine::Store::get_user, py::arg("user_id"))
        .def("save_user", &engine::Store::save_user, py::arg("state"))
        .def(
            "update",
            [](engine::Store& self, long long user_id, const py::kwargs& fields) {
                std::vector<std::pair<std::string, std::optional<std::string>>> converted;
                converted.reserve(fields.size());
                for (auto item : fields) {
                    const auto name = py::cast<std::string>(item.first);
                    if (item.second.is_none()) {
                        converted.emplace_back(name, std::nullopt);
                    } else {
                        converted.emplace_back(name, py::cast<std::string>(item.second));
                    }
                }
                return self.update(user_id, converted);
            },
            py::arg("user_id"))
        .def(
            "remember",
            [](engine::Store& self, long long user_id, std::string session_id, py::object title) {
                std::optional<std::string> converted;
                if (!title.is_none()) {
                    converted = py::cast<std::string>(title);
                }
                self.remember(user_id, session_id, converted);
            },
            py::arg("user_id"), py::arg("session_id"), py::arg("title") = py::none())
        .def("conversation_ids", &engine::Store::conversation_ids, py::arg("user_id"),
             py::arg("limit") = 200)
        .def("forget", &engine::Store::forget, py::arg("user_id"), py::arg("session_id"))
        .def("close", &engine::Store::close);
}
