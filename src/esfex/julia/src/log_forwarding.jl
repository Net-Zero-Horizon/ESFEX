#=
Julia log records → a host callback (the Python logger under juliacall).

Run from Python, Julia's default logger writes to the process's stderr,
outside Python's logging: @info/@warn lines (Benders iterations, warnings)
never reached the ESFEX console handler or the run's log file.
`set_log_callback(callback)` installs a `CallbackLogger` as the global
logger; it hands each record to `callback(level, message, milestone)` with
the level as a Python logging level (10 DEBUG, 20 INFO, 30 WARNING,
40 ERROR).
=#

import Base.CoreLogging: AbstractLogger, LogLevel, handle_message, shouldlog,
    min_enabled_level, catch_exceptions, global_logger

"""
    CallbackLogger(callback; thread = Threads.threadid())

Logger handing every record to `callback(level::Int, message::String,
milestone::Bool)`. `message` is the formatted text with its key = value
pairs; `milestone` marks the Benders progress lines (group `:benders`), which
ESFEX's basic console shows. Debug records are kept only from ESFEX itself
(not from Base or the packages it loads); `maxlog` is honoured.

The callback runs only on `thread`, the one that installed the logger (the
host language is not thread-safe): records from other threads (subproblems
solved in parallel) wait in a queue and go out, in order, before the next
record logged on `thread`, or at `flush_log_queue()`.
"""
mutable struct CallbackLogger <: AbstractLogger
    callback::Any
    thread::Int
    queue::Vector{Tuple{Int, String, Bool}}
    lock::ReentrantLock
    message_limits::Dict{Any, Int}
end

CallbackLogger(callback; thread::Int = Threads.threadid()) = CallbackLogger(
    callback, thread, Tuple{Int, String, Bool}[], ReentrantLock(), Dict{Any, Int}())

# Python logging levels
function host_log_level(level::LogLevel)
    level < Base.CoreLogging.Info && return 10
    level < Base.CoreLogging.Warn && return 20
    level < Base.CoreLogging.Error && return 30
    return 40
end

function _from_esfex(m::Module)
    while true
        m === (@__MODULE__) && return true
        p = parentmodule(m)
        p === m && return false
        m = p
    end
end

min_enabled_level(::CallbackLogger) = Base.CoreLogging.Debug
shouldlog(::CallbackLogger, level, _module, group, id) =
    level >= Base.CoreLogging.Info || _from_esfex(_module)
catch_exceptions(::CallbackLogger) = true

"""
    format_log_message(message, kwargs) -> String

The message and its key = value pairs, one per line (an `exception` with
its error text), as Julia's console logger shows them.
"""
function format_log_message(message, kwargs)
    text = string(message)
    for (k, v) in kwargs
        k === :maxlog && continue
        val = if k === :exception
            v isa Tuple ? sprint(showerror, v...) : sprint(showerror, v)
        else
            string(v)
        end
        text *= "\n  $(k) = $(val)"
    end
    return text
end

function handle_message(logger::CallbackLogger, level, message, _module, group, id,
                        file, line; kwargs...)
    maxlog = get(kwargs, :maxlog, nothing)
    if maxlog isa Integer
        remaining = lock(logger.lock) do
            r = get!(logger.message_limits, id, Int(maxlog))
            logger.message_limits[id] = r - 1
            r
        end
        remaining > 0 || return nothing
    end
    record = (host_log_level(level), format_log_message(message, kwargs),
              group === :benders && level >= Base.CoreLogging.Info)
    if Threads.threadid() == logger.thread
        flush_log_queue(logger)
        _emit(logger, record)
    else
        lock(() -> push!(logger.queue, record), logger.lock)
    end
    return nothing
end

function _emit(logger::CallbackLogger, record)
    try
        logger.callback(record...)
    catch err
        # The host refused it: never lose the line
        println(stderr, record[2], " (log callback failed: ", sprint(showerror, err), ")")
    end
end

"""
    flush_log_queue([logger]) -> Int

Hand the records logged on other threads to the callback (only on the
logger's thread; elsewhere nothing is done). Returns the number sent.
"""
function flush_log_queue(logger::CallbackLogger)
    Threads.threadid() == logger.thread || return 0
    pending = lock(logger.lock) do
        p = copy(logger.queue)
        empty!(logger.queue)
        p
    end
    foreach(r -> _emit(logger, r), pending)
    return length(pending)
end

function flush_log_queue()
    lg = global_logger()
    return lg isa CallbackLogger ? flush_log_queue(lg) : 0
end

"""
    set_log_callback(callback) -> CallbackLogger

Install a [`CallbackLogger`](@ref) on `callback` as Julia's global logger
(called by the Python bridge right after loading ESFEX).
"""
function set_log_callback(callback)
    logger = CallbackLogger(callback)
    global_logger(logger)
    return logger
end
