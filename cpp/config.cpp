#include "config.hpp"

#include <pwd.h>
#include <unistd.h>

#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "json.hpp"
#include "utf8.hpp"

namespace engine {
namespace {

std::string env_raw(const char* name) {
    const char* value = std::getenv(name);
    return value != nullptr ? std::string(value) : std::string();
}

bool env_present(const char* name) { return std::getenv(name) != nullptr; }

std::string strip(const std::string& value) {
    auto cps = utf8::decode(value);
    utf8::strip_in_place(cps);
    return utf8::encode(cps);
}

std::string lower_ascii(std::string value) {
    for (auto& c : value) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return value;
}

// Python's int(raw) tolerates surrounding whitespace and a sign, but rejects
// trailing junk.
std::optional<long long> parse_int(const std::string& raw) {
    const std::string text = strip(raw);
    if (text.empty()) {
        return std::nullopt;
    }
    try {
        std::size_t used = 0;
        const long long value = std::stoll(text, &used, 10);
        if (used != text.size()) {
            return std::nullopt;
        }
        return value;
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

std::optional<double> parse_float(const std::string& raw) {
    const std::string text = strip(raw);
    if (text.empty()) {
        return std::nullopt;
    }
    try {
        std::size_t used = 0;
        const double value = std::stod(text, &used);
        if (used != text.size()) {
            return std::nullopt;
        }
        return value;
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

// str.isdigit() as the allowlist parser relies on it.
bool all_ascii_digits(const std::string& value) {
    if (value.empty()) {
        return false;
    }
    for (const char c : value) {
        if (c < '0' || c > '9') {
            return false;
        }
    }
    return true;
}

bool is_executable(const std::filesystem::path& path) {
    std::error_code ec;
    return ::access(path.c_str(), X_OK) == 0 && std::filesystem::is_regular_file(path, ec);
}

// shutil.which(): the first executable named `name` on PATH.
std::optional<std::string> which(const std::string& name) {
    if (name.find('/') != std::string::npos) {
        return is_executable(name) ? std::optional<std::string>(name) : std::nullopt;
    }
    std::istringstream path_list(env_raw("PATH"));
    std::string dir;
    while (std::getline(path_list, dir, ':')) {
        if (dir.empty()) {
            continue;
        }
        const auto candidate = std::filesystem::path(dir) / name;
        if (is_executable(candidate)) {
            return candidate.string();
        }
    }
    return std::nullopt;
}

std::string shell_quote(const std::string& value) {
    std::string out = "'";
    for (const char c : value) {
        if (c == '\'') {
            out += "'\\''";
        } else {
            out += c;
        }
    }
    out += "'";
    return out;
}

// `opencode service status`, bounded by coreutils timeout the same way
// ensure-opencode.sh bounds it. The original used create_subprocess_exec with a
// 20s asyncio timeout; going through `timeout` avoids hand-rolled fork/wait.
std::string service_status(const std::string& binary) {
    const std::string command = "timeout 20 " + shell_quote(binary) + " service status 2>/dev/null";
    FILE* pipe = ::popen(command.c_str(), "r");
    if (pipe == nullptr) {
        return "";
    }
    std::string out;
    char buffer[4096];
    while (const std::size_t n = std::fread(buffer, 1, sizeof buffer, pipe)) {
        out.append(buffer, n);
        if (out.size() > (1u << 20)) {
            break;
        }
    }
    ::pclose(pipe);
    return out;
}

std::optional<std::string> read_file(const std::filesystem::path& path) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) {
        return std::nullopt;
    }
    std::ostringstream buffer;
    buffer << stream.rdbuf();
    return buffer.str();
}

}  // namespace

bool Config::allowed(long long user_id) const {
    if (allow_any_user) {
        return true;
    }
    return allowed_user_ids.find(user_id) != allowed_user_ids.end();
}

bool env_bool(const char* name, bool fallback) {
    const char* raw = std::getenv(name);
    if (raw == nullptr) {
        return fallback;
    }
    const std::string value = lower_ascii(strip(raw));
    return value == "1" || value == "true" || value == "yes" || value == "on";
}

long long env_int(const char* name, long long fallback) {
    const char* raw = std::getenv(name);
    if (raw == nullptr || *raw == '\0') {
        return fallback;
    }
    return parse_int(raw).value_or(fallback);
}

double env_float(const char* name, double fallback) {
    const char* raw = std::getenv(name);
    if (raw == nullptr || *raw == '\0') {
        return fallback;
    }
    return parse_float(raw).value_or(fallback);
}

std::string home_dir() {
    const std::string home = env_raw("HOME");
    if (!home.empty()) {
        return home;
    }
    if (const struct passwd* entry = ::getpwuid(::getuid());
        entry != nullptr && entry->pw_dir != nullptr) {
        return entry->pw_dir;
    }
    return "";
}

Config config_from_env() {
    Config cfg;

    // "1 2,3" -> {1, 2, 3}; anything that is not all digits is dropped.
    {
        std::string users = env_raw("DISCORD_USER_IDS");
        for (auto& c : users) {
            if (c == ',') {
                c = ' ';
            }
        }
        std::istringstream parts(users);
        std::string part;
        while (parts >> part) {
            if (all_ascii_digits(part)) {
                if (const auto value = parse_int(part)) {
                    cfg.allowed_user_ids.insert(*value);
                }
            }
        }
    }

    cfg.allow_any_user = env_bool("ALLOW_ANY_USER", false);
    cfg.discord_token = strip(env_raw("DISCORD_TOKEN"));
    if (cfg.discord_token.empty()) {
        throw ConfigError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.");
    }
    if (cfg.allowed_user_ids.empty() && !cfg.allow_any_user) {
        throw ConfigError(
            "DISCORD_USER_IDS is empty. Set your numeric Discord user id, or set "
            "ALLOW_ANY_USER=1 to allow everyone (the agent can run tools on this machine, "
            "so a whitelist is strongly recommended).");
    }

    cfg.allowlist_hint = env_bool("ALLOWLIST_HINT", true);

    cfg.opencode_url = strip(env_raw("OPENCODE_URL"));
    if (cfg.opencode_url.empty()) {
        cfg.opencode_url = DEFAULT_SERVICE_URL;
    }
    cfg.opencode_username = strip(env_raw("OPENCODE_USERNAME"));
    if (cfg.opencode_username.empty()) {
        cfg.opencode_username = "opencode";
    }
    cfg.opencode_password = env_raw("OPENCODE_PASSWORD");
    cfg.opencode_directory = strip(env_raw("OPENCODE_DIRECTORY"));

    cfg.default_model = strip(env_raw("DEFAULT_MODEL"));
    cfg.default_effort = strip(env_raw("DEFAULT_EFFORT"));
    cfg.default_agent = strip(env_raw("DEFAULT_AGENT"));
    if (cfg.default_agent.empty()) {
        cfg.default_agent = "build";
    }

    cfg.steer_when_busy = env_bool("STEER_WHEN_BUSY", true);
    cfg.edit_interval = env_float("EDIT_INTERVAL", 1.5);
    cfg.turn_timeout = env_float("TURN_TIMEOUT", 3600.0);
    cfg.stall_timeout = env_float("STALL_TIMEOUT", 300.0);
    cfg.show_tools = env_bool("SHOW_TOOLS", true);
    cfg.show_reasoning = env_bool("SHOW_REASONING", false);
    cfg.session_list_limit = env_int("SESSION_LIST_LIMIT", 100);
    cfg.attachment_dir = strip(env_raw("ATTACHMENT_DIR"));

    if (cfg.opencode_directory.empty()) {
        cfg.opencode_directory = home_dir();
    }
    // attachment_dir defaults to cache_dir()/"attachments", which the binding
    // fills in because that value is a pathlib.Path.

    return cfg;
}

ServiceEndpoint discover_service() {
    ServiceEndpoint endpoint;
    endpoint.url = strip(env_raw("OPENCODE_URL"));
    endpoint.username = strip(env_raw("OPENCODE_USERNAME"));
    if (endpoint.username.empty()) {
        endpoint.username = "opencode";
    }
    endpoint.password = env_raw("OPENCODE_PASSWORD");

    if (endpoint.url.empty()) {
        if (const auto binary = which("opencode")) {
            const std::string out = service_status(*binary);
            if (!out.empty()) {
                auto cps = utf8::decode(out);
                utf8::strip_in_place(cps);
                const auto lines = utf8::split_lines(cps);
                if (!lines.empty()) {
                    const std::string first = strip(utf8::encode(lines.front()));
                    if (first.rfind("http", 0) == 0) {
                        endpoint.url = first;
                    }
                }
            }
        }
    }

    if (endpoint.password.empty()) {
        // Python used os.environ.get with a Path default, so an empty (rather
        // than unset) XDG_CONFIG_HOME resolves relative.
        const std::string config_home =
            env_present("XDG_CONFIG_HOME") ? env_raw("XDG_CONFIG_HOME") : home_dir() + "/.config";

        const std::vector<std::filesystem::path> candidates = {
            std::filesystem::path(config_home) / "opencode" / "service.json",
            std::filesystem::path(home_dir()) / ".config" / "opencode" / "service.json",
        };

        for (const auto& candidate : candidates) {
            const auto raw = read_file(candidate);
            if (!raw) {
                continue;
            }
            try {
                const Json parsed = Json::parse(*raw);
                if (!parsed.is_object()) {
                    continue;
                }
                const auto it = parsed.find("password");
                if (it != parsed.end() && it->is_string()) {
                    endpoint.password = it->get<std::string>();
                }
            } catch (const Json::exception&) {
                continue;
            }
            if (!endpoint.password.empty()) {
                break;
            }
        }
    }

    if (endpoint.url.empty()) {
        endpoint.url = DEFAULT_SERVICE_URL;
    }
    return endpoint;
}

}  // namespace engine
