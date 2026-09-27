#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstddef>
#include <cstdlib>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "config.hpp"
#include "json.hpp"

#include "sse.hpp"
#include "store.hpp"
#include "text.hpp"
#include "turn.hpp"

namespace py = pybind11;

namespace {

// engine::Json -> plain Python objects. Callers poke these with dict.get(),
// isinstance() and truthiness, so they must be real dict/list/scalars and not
// wrapper types.
py::object json_to_py(const engine::Json& value) {
    using value_t = engine::Json::value_t;
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

// Python -> JSON for the event and message payloads the turn state machine
// consumes. These always originate as JSON, so anything unrecognised degrades to
// null rather than raising.
engine::Json py_to_json(py::handle value) {
    if (!value || value.is_none()) {
        return nullptr;
    }
    if (PyBool_Check(value.ptr())) {
        return value.ptr() == Py_True;
    }
    if (PyLong_Check(value.ptr())) {
        return py::cast<long long>(value);
    }
    if (PyFloat_Check(value.ptr())) {
        return py::cast<double>(value);
    }
    if (PyUnicode_Check(value.ptr())) {
        return py::cast<std::string>(value);
    }
    if (PyDict_Check(value.ptr())) {
        engine::Json out = engine::Json::object();
        for (auto item : py::reinterpret_borrow<py::dict>(value)) {
            out[py::cast<std::string>(item.first)] = py_to_json(item.second);
        }
        return out;
    }
    if (PyList_Check(value.ptr()) || PyTuple_Check(value.ptr())) {
        engine::Json out = engine::Json::array();
        for (auto item : value) {
            out.push_back(py_to_json(item));
        }
        return out;
    }
    return nullptr;
}

// The XDG helpers return pathlib.Path, so they are built through Python's own
// pathlib rather than reimplementing its normalisation.
py::object pathlib_path(const std::string& value) {
    return py::module_::import("pathlib").attr("Path")(py::str(value));
}

py::object xdg_dir(const std::string& variable, const std::string& fallback) {
    const char* raw = std::getenv(variable.c_str());
    const py::object stripped = (raw != nullptr ? py::str(raw) : py::str("")).attr("strip")();
    if (PyObject_IsTrue(stripped.ptr()) == 1) {
        return pathlib_path(py::cast<std::string>(stripped));
    }
    return py::module_::import("pathlib")
        .attr("Path")
        .attr("home")()
        .attr("joinpath")(fallback);
}

py::object state_dir() {
    return xdg_dir("XDG_STATE_HOME", ".local/state").attr("joinpath")("opencode-discord");
}

py::object cache_dir() {
    return xdg_dir("XDG_CACHE_HOME", ".cache").attr("joinpath")("opencode-discord");
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
            // Shares token_total() with render_footer, so the two cannot drift.
            return text::format_tokens(engine::token_total(py_to_json(tokens)));
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
                std::vector<engine::Json> events = self.feed(chunk);
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

    // ------------------------------------------------------------------ turn

    py::class_<engine::ToolActivity, std::shared_ptr<engine::ToolActivity>>(
        m, "ToolActivity", "One tool call observed during a turn.")
        .def_readwrite("call_id", &engine::ToolActivity::call_id)
        .def_readwrite("name", &engine::ToolActivity::name)
        .def_readwrite("status", &engine::ToolActivity::status)
        .def_readwrite("detail", &engine::ToolActivity::detail)
        .def_readwrite("error", &engine::ToolActivity::error)
        .def_property_readonly("icon", &engine::ToolActivity::icon)
        .def("label", &engine::ToolActivity::label);

    py::class_<engine::Step, std::shared_ptr<engine::Step>>(
        m, "Step", "One model step: interleaved text, reasoning and tool calls.")
        .def_readwrite("message_id", &engine::Step::message_id)
        .def_readwrite("finished", &engine::Step::finished)
        .def_readwrite("finish", &engine::Step::finish)
        .def_readwrite("error", &engine::Step::error)
        .def_readwrite("cost", &engine::Step::cost)
        .def_readwrite("started", &engine::Step::started)
        // Live ToolActivity handles, keyed by call id, in call order. Sharing
        // ownership means a caller that holds one keeps seeing later mutations.
        .def_property_readonly("tools",
                               [](const engine::Step& self) {
                                   py::dict out;
                                   for (const auto& [call_id, tool] : self.tools) {
                                       out[py::str(call_id)] = tool;
                                   }
                                   return out;
                               })
        .def_property_readonly("tokens",
                               [](const engine::Step& self) { return json_to_py(self.tokens); })
        .def_property_readonly("text", &engine::Step::text)
        .def_property_readonly("thinking", &engine::Step::thinking)
        .def_property_readonly("has_text", &engine::Step::has_text)
        .def_property_readonly("duration", &engine::Step::duration);

    py::class_<engine::TurnState>(m, "TurnState",
                                  "Accumulated state for one execution; consumes the event "
                                  "stream and exposes renderable text and tool activity.")
        .def(py::init([](std::string session_id) {
                 return std::make_unique<engine::TurnState>(std::move(session_id));
             }),
             py::arg("session_id"))
        .def_readwrite("session_id", &engine::TurnState::session_id)
        .def_readwrite("cost", &engine::TurnState::cost)
        .def_readwrite("outcome", &engine::TurnState::outcome)
        .def_readwrite("error", &engine::TurnState::error)
        .def_readwrite("retrying", &engine::TurnState::retrying)
        .def_readwrite("current", &engine::TurnState::current)
        .def_property_readonly("steps",
                               [](const engine::TurnState& self) {
                                   py::list out;
                                   for (const auto& step : self.steps) {
                                       out.append(step);
                                   }
                                   return out;
                               })
        .def_property_readonly("tokens",
                               [](const engine::TurnState& self) { return json_to_py(self.tokens); })
        .def_property_readonly("duration", &engine::TurnState::duration)
        .def_property_readonly("text", &engine::TurnState::text)
        .def_property_readonly("permissions",
                               [](const engine::TurnState& self) {
                                   py::dict out;
                                   for (const auto& [key, payload] : self.permissions) {
                                       out[py::str(key)] = json_to_py(payload);
                                   }
                                   return out;
                               })
        .def_property_readonly("pending_permissions",
                               [](const engine::TurnState& self) {
                                   py::dict out;
                                   for (const auto& [key, payload] : self.permissions) {
                                       out[py::str(key)] = json_to_py(payload);
                                   }
                                   return out;
                               })
        .def("step_for", &engine::TurnState::step_for, py::arg("message_id"))
        .def("tool_names", &engine::TurnState::tool_names);

    m.def(
        "apply_event",
        [](engine::TurnState& state, py::object event) { engine::apply_event(state, py_to_json(event)); },
        py::arg("state"), py::arg("event"), "Fold one OpenCode event into the turn state.");

    m.def(
        "hydrate_from_message",
        [](engine::TurnState& state, py::object message) {
            engine::hydrate_from_message(state, py_to_json(message));
        },
        py::arg("state"), py::arg("message"),
        "Fill in authoritative tool names/statuses from a fetched message.");

    m.def(
        "tool_detail",
        [](py::object payload, std::size_t width) {
            return engine::tool_detail(py_to_json(payload), width);
        },
        py::arg("payload"), py::arg("width") = 42);

    m.def("render_status", &engine::render_status, py::arg("state"), py::arg("model_label") = "");
    m.def("render_reasoning", &engine::render_reasoning, py::arg("text"), py::arg("limit") = 700);
    m.def("render_tools", &engine::render_tools, py::arg("step"), py::arg("limit") = 300);
    m.def("render_footer", &engine::render_footer, py::arg("state"), py::arg("step"),
          py::arg("model_label") = "");

    // ---------------------------------------------------------------- config

    m.attr("DEFAULT_SERVICE_URL") = engine::DEFAULT_SERVICE_URL;

    py::class_<engine::Config>(m, "Config",
                               "Runtime configuration: env knobs, their defaults and the "
                               "fail-closed startup checks.")
        .def_readwrite("discord_token", &engine::Config::discord_token)
        .def_readwrite("allow_any_user", &engine::Config::allow_any_user)
        .def_readwrite("allowlist_hint", &engine::Config::allowlist_hint)
        .def_readwrite("opencode_url", &engine::Config::opencode_url)
        .def_readwrite("opencode_username", &engine::Config::opencode_username)
        .def_readwrite("opencode_password", &engine::Config::opencode_password)
        .def_readwrite("opencode_directory", &engine::Config::opencode_directory)
        .def_readwrite("default_model", &engine::Config::default_model)
        .def_readwrite("default_effort", &engine::Config::default_effort)
        .def_readwrite("default_agent", &engine::Config::default_agent)
        .def_readwrite("steer_when_busy", &engine::Config::steer_when_busy)
        .def_readwrite("edit_interval", &engine::Config::edit_interval)
        .def_readwrite("turn_timeout", &engine::Config::turn_timeout)
        .def_readwrite("stall_timeout", &engine::Config::stall_timeout)
        .def_readwrite("show_tools", &engine::Config::show_tools)
        .def_readwrite("show_reasoning", &engine::Config::show_reasoning)
        .def_readwrite("session_list_limit", &engine::Config::session_list_limit)
        .def_readwrite("attachment_dir", &engine::Config::attachment_dir)
        .def_property(
            "allowed_user_ids",
            [](const engine::Config& self) {
                py::set out;
                for (const long long id : self.allowed_user_ids) {
                    out.add(py::int_(id));
                }
                return out;
            },
            [](engine::Config& self, const py::object& value) {
                self.allowed_user_ids.clear();
                for (py::handle item : value) {
                    self.allowed_user_ids.insert(py::cast<long long>(item));
                }
            })
        .def("allowed", &engine::Config::allowed, py::arg("user_id"))
        .def_static("from_env", []() {
            engine::Config cfg = [] {
                try {
                    return engine::config_from_env();
                } catch (const engine::ConfigError& exc) {
                    // The original raises SystemExit for these two checks.
                    PyErr_SetString(PyExc_SystemExit, exc.what());
                    throw py::error_already_set();
                }
            }();
            if (cfg.attachment_dir.empty()) {
                cfg.attachment_dir = py::cast<std::string>(
                    py::str(cache_dir().attr("joinpath")("attachments")));
            }
            return cfg;
        });

    m.def("xdg_dir", &xdg_dir, py::arg("variable"), py::arg("default"));
    m.def("state_dir", &state_dir);
    m.def("cache_dir", &cache_dir);

    m.def("discover_service", []() {
        const engine::ServiceEndpoint endpoint = engine::discover_service();
        return py::make_tuple(endpoint.url, endpoint.username, endpoint.password);
    });
}
