#pragma once

// Runtime configuration for the OpenCode Discord bridge.
//
// Port of the parsing half of bot/config.py: the env knobs, their defaults and
// the fail-closed startup checks, plus best-effort discovery of the local
// OpenCode service.
//
// The XDG path helpers stay out of here: they return pathlib.Path objects, so
// they are built in the binding layer where pathlib is reachable.

#include <cstdint>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>

namespace engine {

inline constexpr const char* DEFAULT_SERVICE_URL = "http://127.0.0.1:4096";

// Raised for the fail-closed startup checks; the binding turns this into
// SystemExit to match the original.
class ConfigError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct Config {
    std::string discord_token;
    std::set<long long> allowed_user_ids;
    bool allow_any_user = false;
    bool allowlist_hint = true;

    std::string opencode_url = DEFAULT_SERVICE_URL;
    std::string opencode_username = "opencode";
    std::string opencode_password;
    std::string opencode_directory;

    std::string default_model;
    std::string default_effort;
    std::string default_agent = "build";

    bool steer_when_busy = true;
    double edit_interval = 1.5;
    double turn_timeout = 3600.0;
    double stall_timeout = 300.0;
    bool show_tools = true;
    bool show_reasoning = false;
    long long session_list_limit = 100;
    std::string attachment_dir;

    bool allowed(long long user_id) const;
};

// Every knob below comes from the environment; malformed values fall back to the
// default rather than raising, exactly as Python's int()/float() try-except did.
bool env_bool(const char* name, bool fallback);
long long env_int(const char* name, long long fallback);
double env_float(const char* name, double fallback);

// Reads the environment and applies the fail-closed checks, throwing ConfigError
// when the token or the allowlist is missing.
Config config_from_env();

struct ServiceEndpoint {
    std::string url;
    std::string username;
    std::string password;
};

// Best-effort discovery of the local service. Environment variables always win,
// so this only fills in what is missing.
ServiceEndpoint discover_service();

// The user's home directory, as os.path.expanduser("~") would resolve it.
std::string home_dir();

}  // namespace engine
