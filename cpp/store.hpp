#pragma once

// SQLite persistence for per-user chat state.
//
// Port of bot/store.py. One row per Discord user holds the active conversation
// plus the model / effort / agent preferences, and a join table remembers every
// conversation the user has used through the bot.
//
// The schema and stored value types are byte-compatible with the Python
// original so an existing state.db keeps working: timestamps stay REAL Unix
// epoch seconds.

#include <cstdint>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

struct sqlite3;

namespace engine {

struct UserState {
    long long user_id = 0;
    std::optional<std::string> session_id;
    std::optional<std::string> provider_id;
    std::optional<std::string> model_id;
    std::optional<std::string> variant;
    std::optional<std::string> agent;
    std::optional<std::string> directory;
    std::optional<std::string> title;

    // bool(provider_id and model_id): an empty string is absent, as in Python.
    bool has_model() const;

    // "default", or "{provider}/{model}" with " · {variant}" appended when the
    // variant is set and is not "default".
    std::string model_label() const;
};

class Store {
public:
    explicit Store(std::string path);
    ~Store();

    Store(const Store&) = delete;
    Store& operator=(const Store&) = delete;

    const std::string& path() const { return path_; }

    UserState get_user(long long user_id);

    void save_user(const UserState& state);

    // Applies the named fields (attribute names, as in Python's **fields) to the
    // stored row and returns the result. Unknown names and "user_id" are ignored,
    // matching hasattr()-based reflection in the original.
    UserState update(long long user_id,
                     const std::vector<std::pair<std::string, std::optional<std::string>>>& fields);

    void remember(long long user_id, const std::string& session_id,
                  const std::optional<std::string>& title);

    std::vector<std::string> conversation_ids(long long user_id, long long limit = 200);

    void forget(long long user_id, const std::string& session_id);

    void close();

private:
    void init_schema();
    UserState get_user_locked(long long user_id);
    void save_user_locked(const UserState& state);

    // Python relies on the GIL serialising access from one event-loop thread.
    // C++ has no such guarantee, so every call takes this.
    std::mutex mutex_;
    sqlite3* db_ = nullptr;
    std::string path_;
};

}  // namespace engine
