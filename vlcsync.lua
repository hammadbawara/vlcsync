local host = "127.0.0.1"
local port = tonumber(config["port"]) or 4213
local running = true
local msg_terminator = "\n"


local function get_status()
    local input = vlc.object.input()
    if not input then
        return "no-input", "no-input"
    end

    local status = vlc.playlist.status()
    if status ~= "playing" then
        status = "paused"
    end

    local t = vlc.var.get(input, "time")
    if tonumber(vlc.misc.version():sub(1, 1)) > 2 then
        t = t / 1000000
    end
    return status, tostring(t)
end


local function set_time(seconds)
    local input = vlc.object.input()
    if not input then
        return "error:no-input"
    end

    local sec = tonumber(seconds)
    if sec == nil then
        return "error:bad-seek"
    end

    if tonumber(vlc.misc.version():sub(1, 1)) > 2 then
        sec = sec * 1000000
    end
    vlc.var.set(input, "time", sec)
    return "ok"
end


local function set_playstate(desired)
    local input = vlc.object.input()
    if not input then
        return "error:no-input"
    end

    local current = vlc.playlist.status()
    if current ~= "playing" then
        current = "paused"
    end

    if desired == "play" and current ~= "playing" then
        vlc.playlist.pause()
    elseif desired == "pause" and current ~= "paused" then
        vlc.playlist.pause()
    end
    return "ok"
end


local function handle_command(cmd)
    if cmd == "state" then
        local status, position = get_status()
        return "state:" .. status .. ";position:" .. position
    end

    if cmd == "play" then
        return set_playstate("play")
    end

    if cmd == "pause" then
        return set_playstate("pause")
    end

    if string.find(cmd, "seek:") == 1 then
        local value = string.sub(cmd, 6)
        return set_time(value)
    end

    if cmd == "ping" then
        return "pong"
    end

    return "error:unknown-command"
end


local listener = vlc.net.listen_tcp(host, port)
vlc.msg.info("vlcsync interface listening on " .. host .. ":" .. port)


while running do
    local fd = listener:accept()
    local input_buffer = ""
    while fd >= 0 and running do
        local chunk = vlc.net.recv(fd, 1000)
        if chunk == nil then
            chunk = ""
        end

        input_buffer = input_buffer .. string.gsub(chunk, "\r", "")
        while string.find(input_buffer, msg_terminator) do
            local idx = string.find(input_buffer, msg_terminator)
            local request = string.sub(input_buffer, 1, idx - 1)
            input_buffer = string.sub(input_buffer, idx + 1)

            local response = handle_command(request)
            vlc.net.send(fd, response .. msg_terminator)
        end

        vlc.misc.mwait(vlc.misc.mdate() + 10000)
        if vlc.volume.get() == -256 then
            running = false
        end
    end
end