/**
 * command_server.hpp —— AFS4 Bridge DLL 命令输入端口
 * ================================================================
 * 监听 localhost:12346，接收 JSON 格式的控制命令，
 * 解析后通过 AFS4 API 写入模拟器，返回 JSON 响应。
 *
 * 命令格式（与规格书 Module 1 一致）：
 *   {"type":"control", "command":"set_xpdr_mode", "params":{"mode":"ALT"}}
 *
 * 支持的命令：
 *   - set_xpdr_code   {"code":"1234"}      设置应答机代码
 *   - set_xpdr_mode   {"mode":"ALT"}       设置应答机模式（OFF/SBY/ON/ALT）
 *   - set_com1_freq   {"freq":122800}      设置 COM1 频率（kHz*10）
 *   - set_com2_freq   {"freq":119500}      设置 COM2 频率
 *   - ident           {"duration":5}       触发 IDENT 信号
 *
 * 响应格式：
 *   {"status":"ok"}           写入成功
 *   {"status":"unsupported"}  AFS4 不支持此操作（客户端将降级到虚拟模式）
 *   {"status":"error","msg":"..."}  其他错误
 *
 * 协议：每条命令和响应均为一行 JSON（以 \n 分隔）。
 */
#pragma once

#include "json.hpp"
#include "afs4_api.hpp"

#include <WinSock2.h>
#include <WS2tcpip.h>
#include <windows.h>

#include <thread>
#include <atomic>
#include <mutex>
#include <vector>
#include <string>
#include <memory>
#include <functional>
#include <unordered_set>
#include <iostream>

namespace afs4 {

// ════════════════════════════════════════════════════════════════════
//  CommandServer —— 命令输入端口 TCP 服务器
// ════════════════════════════════════════════════════════════════════

class CommandServer {
public:
    static constexpr int  DEFAULT_PORT = 12346;
    static constexpr auto DEFAULT_HOST = "127.0.0.1";

    /// 日志回调类型
    using LogFn = std::function<void(const std::string&)>;

    /**
     * @param api  AFS4 API 实例（真实或 Mock）
     * @param port 监听端口（默认 12346）
     * @param host 监听地址（默认 127.0.0.1，仅本地）
     */
    CommandServer(std::shared_ptr<IAFS4Api> api,
                  int port = DEFAULT_PORT,
                  const std::string& host = DEFAULT_HOST)
        : api_(std::move(api))
        , port_(port)
        , host_(host)
        , running_(false)
        , listen_socket_(INVALID_SOCKET)
    {
        // 默认日志输出到 stdout
        log_fn_ = [](const std::string& msg) {
            std::cout << "[CmdServer] " << msg << std::endl;
        };
    }

    ~CommandServer() { stop(); }

    /// 设置日志回调
    void set_log_fn(LogFn fn) { log_fn_ = std::move(fn); }

    /// 启动服务器（非阻塞，内部创建线程）
    bool start() {
        if (running_.load()) return true;

        // ── Winsock 初始化 ──
        WSADATA wsa;
        int rc = WSAStartup(MAKEWORD(2, 2), &wsa);
        if (rc != 0) {
            log("WSAStartup 失败: " + std::to_string(rc));
            return false;
        }

        // ── 创建监听 socket ──
        listen_socket_ = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (listen_socket_ == INVALID_SOCKET) {
            log("socket() 失败: " + std::to_string(WSAGetLastError()));
            return false;
        }

        // 允许地址重用（重启时避免 TIME_WAIT 阻塞）
        BOOL reuse = TRUE;
        setsockopt(listen_socket_, SOL_SOCKET, SO_REUSEADDR,
                   reinterpret_cast<const char*>(&reuse), sizeof(reuse));

        // ── 绑定 ──
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(static_cast<u_short>(port_));
        inet_pton(AF_INET, host_.c_str(), &addr.sin_addr);

        rc = bind(listen_socket_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
        if (rc == SOCKET_ERROR) {
            log("bind() 失败: " + std::to_string(WSAGetLastError())
                + " (port=" + std::to_string(port_) + ")");
            closesocket(listen_socket_);
            listen_socket_ = INVALID_SOCKET;
            return false;
        }

        // ── 监听 ──
        rc = listen(listen_socket_, SOMAXCONN);
        if (rc == SOCKET_ERROR) {
            log("listen() 失败: " + std::to_string(WSAGetLastError()));
            closesocket(listen_socket_);
            listen_socket_ = INVALID_SOCKET;
            return false;
        }

        running_.store(true);
        accept_thread_ = std::thread(&CommandServer::accept_loop, this);
        log("命令输入端口已启动: " + host_ + ":" + std::to_string(port_));
        return true;
    }

    /// 停止服务器
    void stop() {
        if (!running_.load()) return;
        running_.store(false);

        if (listen_socket_ != INVALID_SOCKET) {
            // 关闭监听 socket，使 accept() 返回
            closesocket(listen_socket_);
            listen_socket_ = INVALID_SOCKET;
        }

        // 关闭所有客户端连接
        {
            std::lock_guard<std::mutex> lock(clients_mtx_);
            for (SOCKET s : client_sockets_) {
                if (s != INVALID_SOCKET) closesocket(s);
            }
            client_sockets_.clear();
        }

        if (accept_thread_.joinable())
            accept_thread_.join();

        WSACleanup();
        log("命令输入端口已停止");
    }

    bool is_running() const { return running_.load(); }

private:
    // ── 接受连接循环 ──
    void accept_loop() {
        while (running_.load()) {
            sockaddr_in client_addr{};
            int addr_len = sizeof(client_addr);
            SOCKET client = accept(listen_socket_,
                reinterpret_cast<sockaddr*>(&client_addr), &addr_len);

            if (client == INVALID_SOCKET) {
                if (running_.load()) {
                    log("accept() 失败: " + std::to_string(WSAGetLastError()));
                }
                break;
            }

            // 设置发送/接收超时（3 秒）
            DWORD timeout_ms = 3000;
            setsockopt(client, SOL_SOCKET, SO_RCVTIMEO,
                       reinterpret_cast<const char*>(&timeout_ms), sizeof(timeout_ms));

            {
                std::lock_guard<std::mutex> lock(clients_mtx_);
                client_sockets_.insert(client);
            }

            char ip[INET_ADDRSTRLEN];
            inet_ntop(AF_INET, &client_addr.sin_addr, ip, sizeof(ip));
            log("命令客户端已连接: " + std::string(ip)
                + ":" + std::to_string(ntohs(client_addr.sin_port)));

            // 每个连接一个处理线程
            std::thread(&CommandServer::handle_client, this, client).detach();
        }
    }

    // ── 单个客户端处理循环 ──
    void handle_client(SOCKET client) {
        std::string recv_buf;

        while (running_.load()) {
            char chunk[4096];
            int n = recv(client, chunk, sizeof(chunk), 0);

            if (n <= 0) {
                if (n == 0) {
                    log("命令客户端断开连接");
                } else {
                    int err = WSAGetLastError();
                    if (err != WSAETIMEDOUT) {
                        log("recv() 错误: " + std::to_string(err));
                    }
                    if (err == WSAETIMEDOUT) continue;  // 超时，继续等待
                }
                break;
            }

            recv_buf.append(chunk, n);

            // 按行处理（每行一条 JSON 命令）
            size_t pos;
            while ((pos = recv_buf.find('\n')) != std::string::npos) {
                std::string line = recv_buf.substr(0, pos);
                recv_buf.erase(0, pos + 1);

                // 去除 \r
                if (!line.empty() && line.back() == '\r')
                    line.pop_back();

                if (line.empty()) continue;

                // 解析并处理命令
                std::string response = process_command(line);

                // 发送响应（以 \n 分隔）
                response += "\n";
                send(client, response.c_str(),
                     static_cast<int>(response.size()), 0);
            }
        }

        {
            std::lock_guard<std::mutex> lock(clients_mtx_);
            client_sockets_.erase(client);
        }
        closesocket(client);
    }

    // ── 命令处理核心 ──
    std::string process_command(const std::string& line) {
        json::Value resp;

        try {
            json::Value cmd = json::parse(line);

            // 验证命令格式
            std::string type = cmd["type"].as_string();
            std::string command = cmd["command"].as_string();

            if (type != "control" || command.empty()) {
                resp = json::Object{{"status", "error"},
                                    {"msg", "invalid command format"}};
                return resp.dump();
            }

            const json::Value& params = cmd["params"];

            log("收到命令: " + command);

            // ── 分发命令 ──
            if (command == "set_xpdr_code") {
                resp = handle_set_xpdr_code(params);
            } else if (command == "set_xpdr_mode") {
                resp = handle_set_xpdr_mode(params);
            } else if (command == "set_com1_freq") {
                resp = handle_set_com_freq(params, 1);
            } else if (command == "set_com2_freq") {
                resp = handle_set_com_freq(params, 2);
            } else if (command == "ident") {
                resp = handle_ident(params);
            } else {
                resp = json::Object{{"status", "error"},
                                    {"msg", "unknown command: " + command}};
            }

        } catch (const std::exception& e) {
            resp = json::Object{{"status", "error"},
                                {"msg", std::string("parse error: ") + e.what()}};
        }

        log("响应: " + resp.dump());
        return resp.dump();
    }

    // ── 各命令处理器 ──

    /// set_xpdr_code: 设置应答机代码
    json::Value handle_set_xpdr_code(const json::Value& params) {
        std::string code = params["code"].as_string();
        if (code.empty()) {
            return json::Object{{"status", "error"}, {"msg", "missing 'code' param"}};
        }

        // 验证：4 位八进制（0-7）
        if (code.size() != 4) {
            return json::Object{{"status", "error"},
                                {"msg", "code must be 4 digits"}};
        }
        for (char c : code) {
            if (c < '0' || c > '7') {
                return json::Object{{"status", "error"},
                                    {"msg", "code must be octal (0-7)"}};
            }
        }

        WriteStatus st = api_->set_xpdr_code(code);
        return make_status_response(st, "set_xpdr_code");
    }

    /// set_xpdr_mode: 设置应答机模式
    /// 注意：AFS4 可能不支持外部写入模式，此时返回 unsupported
    json::Value handle_set_xpdr_mode(const json::Value& params) {
        std::string mode = params["mode"].as_string();
        if (mode.empty()) {
            return json::Object{{"status", "error"}, {"msg", "missing 'mode' param"}};
        }

        // 转大写
        for (auto& c : mode) c = toupper(c);

        // 验证合法模式
        if (mode != "OFF" && mode != "SBY" && mode != "ON" && mode != "ALT") {
            return json::Object{{"status", "error"},
                                {"msg", "invalid mode: " + mode}};
        }

        WriteStatus st = api_->set_xpdr_mode(mode);
        return make_status_response(st, "set_xpdr_mode");
    }

    /// set_com1_freq / set_com2_freq: 设置 COM 频率
    json::Value handle_set_com_freq(const json::Value& params, int com_id) {
        if (params["freq"].is_null()) {
            return json::Object{{"status", "error"}, {"msg", "missing 'freq' param"}};
        }
        int freq = params["freq"].as_int();
        if (freq < 100000 || freq > 200000) {
            return json::Object{{"status", "error"},
                                {"msg", "freq out of range (100000-200000)"}};
        }

        WriteStatus st = (com_id == 1)
            ? api_->set_com1_freq(freq)
            : api_->set_com2_freq(freq);
        return make_status_response(st, com_id == 1 ? "set_com1_freq" : "set_com2_freq");
    }

    /// ident: 触发 IDENT 信号
    json::Value handle_ident(const json::Value& params) {
        int duration = params["duration"].is_null() ? 5 : params["duration"].as_int();
        if (duration < 1 || duration > 30) {
            return json::Object{{"status", "error"},
                                {"msg", "duration must be 1-30 seconds"}};
        }

        WriteStatus st = api_->trigger_ident(duration);
        return make_status_response(st, "ident");
    }

    /// 将 WriteStatus 转为 JSON 响应
    json::Value make_status_response(WriteStatus st, const std::string& cmd_name) {
        json::Object resp{{"status", json::Value(std::string(status_str(st)))}};
        if (st == WriteStatus::UNSUPPORTED) {
            resp["msg"] = json::Value(
                cmd_name + " is not supported by this AFS4 version/aircraft");
        } else if (st == WriteStatus::ERR) {
            resp["msg"] = json::Value(cmd_name + " write error");
        }
        return resp;
    }

    void log(const std::string& msg) {
        if (log_fn_) log_fn_(msg);
    }

    // ── 成员 ──
    std::shared_ptr<IAFS4Api> api_;
    int port_;
    std::string host_;
    std::atomic<bool> running_;
    SOCKET listen_socket_;
    std::thread accept_thread_;
    std::mutex clients_mtx_;
    std::unordered_set<SOCKET> client_sockets_;
    LogFn log_fn_;
};

}  // namespace afs4
