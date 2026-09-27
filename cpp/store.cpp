#include "store.hpp"

#include <chrono>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

#include <sqlite3.h>

namespace engine {
namespace {

// time.time(): epoch seconds as a double, which is what the REAL columns hold.
double now_seconds() {
    const auto now = std::chrono::system_clock::now().time_since_epoch();
    return std::chrono::duration<double>(now).count();
}

[[noreturn]] void fail(sqlite3* db, const std::string& what) {
    throw std::runtime_error("sqlite " + what + " failed: " +
                             (db != nullptr ? sqlite3_errmsg(db) : "no handle"));
}

class Statement {
public:
    Statement(sqlite3* db, const char* sql) : db_(db) {
        if (sqlite3_prepare_v2(db, sql, -1, &stmt_, nullptr) != SQLITE_OK) {
            fail(db_, "prepare");
        }
    }

    ~Statement() {
        if (stmt_ != nullptr) {
            sqlite3_finalize(stmt_);
        }
    }

    Statement(const Statement&) = delete;
    Statement& operator=(const Statement&) = delete;

    sqlite3_stmt* get() const { return stmt_; }

    void bind_int64(int index, long long value) {
        if (sqlite3_bind_int64(stmt_, index, value) != SQLITE_OK) {
            fail(db_, "bind");
        }
    }

    void bind_double(int index, double value) {
        if (sqlite3_bind_double(stmt_, index, value) != SQLITE_OK) {
            fail(db_, "bind");
        }
    }

    void bind_text(int index, const std::string& value) {
        if (sqlite3_bind_text(stmt_, index, value.c_str(), static_cast<int>(value.size()),
                              SQLITE_TRANSIENT) != SQLITE_OK) {
            fail(db_, "bind");
        }
    }

    void bind_nullable_text(int index, const std::optional<std::string>& value) {
        if (value.has_value()) {
            bind_text(index, *value);
        } else if (sqlite3_bind_null(stmt_, index) != SQLITE_OK) {
            fail(db_, "bind");
        }
    }

    void step_done() {
        if (sqlite3_step(stmt_) != SQLITE_DONE) {
            fail(db_, "step");
        }
    }

private:
    sqlite3* db_ = nullptr;
    sqlite3_stmt* stmt_ = nullptr;
};

std::optional<std::string> column_optional_text(sqlite3_stmt* stmt, int index) {
    if (sqlite3_column_type(stmt, index) == SQLITE_NULL) {
        return std::nullopt;
    }
    const auto* text = sqlite3_column_text(stmt, index);
    const int size = sqlite3_column_bytes(stmt, index);
    return std::string(reinterpret_cast<const char*>(text), static_cast<std::size_t>(size));
}

}  // namespace

bool UserState::has_model() const {
    return provider_id.has_value() && !provider_id->empty() && model_id.has_value() &&
           !model_id->empty();
}

std::string UserState::model_label() const {
    if (!model_id.has_value() || model_id->empty()) {
        return "default";
    }
    // f"{provider_id}/{model_id}" renders None as the literal "None".
    std::string label = (provider_id.has_value() ? *provider_id : std::string("None")) + "/" +
                        *model_id;
    if (variant.has_value() && !variant->empty() && *variant != "default") {
        label += " \u00b7 " + *variant;
    }
    return label;
}

Store::Store(std::string path) : path_(std::move(path)) {
    if (path_ != ":memory:") {
        const std::filesystem::path target(path_);
        if (target.has_parent_path()) {
            std::error_code ec;
            std::filesystem::create_directories(target.parent_path(), ec);
        }
    }

    const int flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE;
    if (sqlite3_open_v2(path_.c_str(), &db_, flags, nullptr) != SQLITE_OK) {
        fail(db_, "open");
    }

    // Match the original's pragmas. In-memory databases ignore WAL.
    sqlite3_exec(db_, "PRAGMA journal_mode=WAL", nullptr, nullptr, nullptr);
    sqlite3_exec(db_, "PRAGMA synchronous=NORMAL", nullptr, nullptr, nullptr);

    init_schema();
}

Store::~Store() { close(); }

void Store::init_schema() {
    static const char* kSchema = R"sql(
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            session_id  TEXT,
            provider_id TEXT,
            model_id    TEXT,
            variant     TEXT,
            agent       TEXT,
            directory   TEXT,
            title       TEXT,
            updated_at  REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS conversations (
            user_id    INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            title      TEXT,
            last_seen  REAL NOT NULL,
            PRIMARY KEY (user_id, session_id)
        );

        CREATE INDEX IF NOT EXISTS conversations_user
            ON conversations (user_id, last_seen DESC);
    )sql";

    char* error = nullptr;
    if (sqlite3_exec(db_, kSchema, nullptr, nullptr, &error) != SQLITE_OK) {
        const std::string message = error != nullptr ? error : "unknown";
        sqlite3_free(error);
        throw std::runtime_error("sqlite schema init failed: " + message);
    }
}

UserState Store::get_user(long long user_id) {
    const std::lock_guard<std::mutex> lock(mutex_);
    return get_user_locked(user_id);
}

UserState Store::get_user_locked(long long user_id) {
    Statement stmt(db_,
                   "SELECT user_id, session_id, provider_id, model_id, variant, agent, directory, "
                   "title FROM users WHERE user_id = ?");
    stmt.bind_int64(1, user_id);

    const int rc = sqlite3_step(stmt.get());
    if (rc == SQLITE_DONE) {
        UserState empty;
        empty.user_id = user_id;
        return empty;
    }
    if (rc != SQLITE_ROW) {
        fail(db_, "step");
    }

    UserState state;
    state.user_id = sqlite3_column_int64(stmt.get(), 0);
    state.session_id = column_optional_text(stmt.get(), 1);
    state.provider_id = column_optional_text(stmt.get(), 2);
    state.model_id = column_optional_text(stmt.get(), 3);
    state.variant = column_optional_text(stmt.get(), 4);
    state.agent = column_optional_text(stmt.get(), 5);
    state.directory = column_optional_text(stmt.get(), 6);
    state.title = column_optional_text(stmt.get(), 7);
    return state;
}

void Store::save_user(const UserState& state) {
    const std::lock_guard<std::mutex> lock(mutex_);
    save_user_locked(state);
}

void Store::save_user_locked(const UserState& state) {
    Statement stmt(db_, R"sql(
        INSERT INTO users
            (user_id, session_id, provider_id, model_id, variant, agent, directory, title, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            session_id = excluded.session_id,
            provider_id = excluded.provider_id,
            model_id = excluded.model_id,
            variant = excluded.variant,
            agent = excluded.agent,
            directory = excluded.directory,
            title = excluded.title,
            updated_at = excluded.updated_at
    )sql");

    stmt.bind_int64(1, state.user_id);
    stmt.bind_nullable_text(2, state.session_id);
    stmt.bind_nullable_text(3, state.provider_id);
    stmt.bind_nullable_text(4, state.model_id);
    stmt.bind_nullable_text(5, state.variant);
    stmt.bind_nullable_text(6, state.agent);
    stmt.bind_nullable_text(7, state.directory);
    stmt.bind_nullable_text(8, state.title);
    stmt.bind_double(9, now_seconds());
    stmt.step_done();
}

UserState Store::update(
    long long user_id,
    const std::vector<std::pair<std::string, std::optional<std::string>>>& fields) {
    const std::lock_guard<std::mutex> lock(mutex_);

    UserState state = get_user_locked(user_id);
    for (const auto& [name, value] : fields) {
        if (name == "session_id") {
            state.session_id = value;
        } else if (name == "provider_id") {
            state.provider_id = value;
        } else if (name == "model_id") {
            state.model_id = value;
        } else if (name == "variant") {
            state.variant = value;
        } else if (name == "agent") {
            state.agent = value;
        } else if (name == "directory") {
            state.directory = value;
        } else if (name == "title") {
            state.title = value;
        }
        // Anything else (including "user_id") is silently ignored.
    }
    save_user_locked(state);
    return state;
}

void Store::remember(long long user_id, const std::string& session_id,
                     const std::optional<std::string>& title) {
    const std::lock_guard<std::mutex> lock(mutex_);

    Statement stmt(db_, R"sql(
        INSERT INTO conversations (user_id, session_id, title, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, session_id) DO UPDATE SET
            title = excluded.title,
            last_seen = excluded.last_seen
    )sql");

    stmt.bind_int64(1, user_id);
    stmt.bind_text(2, session_id);
    stmt.bind_nullable_text(3, title);
    stmt.bind_double(4, now_seconds());
    stmt.step_done();
}

std::vector<std::string> Store::conversation_ids(long long user_id, long long limit) {
    const std::lock_guard<std::mutex> lock(mutex_);

    // rowid is a tiebreaker the original lacks: two remembers inside the same
    // clock tick would otherwise order non-deterministically, which the port
    // makes more likely by being faster.
    Statement stmt(db_,
                   "SELECT session_id FROM conversations WHERE user_id = ? "
                   "ORDER BY last_seen DESC, rowid DESC LIMIT ?");
    stmt.bind_int64(1, user_id);
    stmt.bind_int64(2, limit);

    std::vector<std::string> ids;
    while (true) {
        const int rc = sqlite3_step(stmt.get());
        if (rc == SQLITE_DONE) {
            break;
        }
        if (rc != SQLITE_ROW) {
            fail(db_, "step");
        }
        ids.push_back(column_optional_text(stmt.get(), 0).value_or(""));
    }
    return ids;
}

void Store::forget(long long user_id, const std::string& session_id) {
    const std::lock_guard<std::mutex> lock(mutex_);

    Statement stmt(db_, "DELETE FROM conversations WHERE user_id = ? AND session_id = ?");
    stmt.bind_int64(1, user_id);
    stmt.bind_text(2, session_id);
    stmt.step_done();
}

void Store::close() {
    const std::lock_guard<std::mutex> lock(mutex_);
    if (db_ != nullptr) {
        sqlite3_close(db_);
        db_ = nullptr;
    }
}

}  // namespace engine
