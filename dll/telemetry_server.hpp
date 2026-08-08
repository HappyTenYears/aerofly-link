/**
 * telemetry_server.hpp —— AFS4 Bridge DLL 遥测数据输出端口
 * ================================================================
 * 监听 localhost:12345，以 ~20Hz（每 50ms）向已连接客户端推送
 * JSON 格式的飞行遥测数据。
 *
 * 输出格式（与规格书 Module 1 一致）：
 *   {"type":"afs4_telemetry","timestamp":1722849600.123,"data":{...}}
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
#include <memory>
#include <functional>
#include <iostream>
#include <chrono>

namespace afs4 {

class TelemetryServer {
public:
    static constexpr int  DEFAULT_PORT = 12345;
    static constexpr auto DEFAULT_HOST = "127.0.0.1";
    static constexpr int  SEND_INTERVAL_MS = 50;   // 20Hz

    using LogFn = std::function<void(const std::string&)>;

    TelemetryServer(std::shared_ptr<IAFS4Api> api,
                    int port = DEFAULT_PORT,
                    const std::string& host = DEFAULT_HOST)
        : api_(std::move(api))
        , port_(port)
        , host_(host)
        , running_(false)
        , listen_socket_(INVALID_SOCKET)
    {
        log_fn_ = [](const std::string& msg) {
            std::cout << "[TelServer] " << msg << std::endl;
        };
    }

    ~TelemetryServer() { stop(); }

    void set_log_fn(LogFn fn) { log_fn_ = std::move(fn); }

    bool start() {
        if (running_.load()) return true;

        WSADATA wsa;
        if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return false;

        listen_socket_ = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (listen_socket_ == INVALID_SOCKET) return false;

        BOOL reuse = TRUE;
        setsockopt(listen_socket_, SOL_SOCKET, SO_REUSEADDR,
                   reinterpret_cast<const char*>(&reuse), sizeof(reuse));

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(static_cast<u_short>(port_));
        inet_pton(AF_INET, host_.c_str(), &addr.sin_addr);

        if (bind(listen_socket_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == SOCKET_ERROR) {
            closesocket(listen_socket_);
            return false;
        }
        if (listen(listen_socket_, SOMAXCONN) == SOCKET_ERROR) {
            closesocket(listen_socket_);
            return false;
        }

        running_.store(true);
        accept_thread_ = std::thread(&TelemetryServer::accept_loop, this);
        send_thread_   = std::thread(&TelemetryServer::send_loop, this);
        log("遥测输出端口已启动: " + host_ + ":" + std::to_string(port_));
        return true;
    }

    void stop() {
        if (!running_.load()) return;
        running_.store(false);

        if (listen_socket_ != INVALID_SOCKET) {
            closesocket(listen_socket_);
            listen_socket_ = INVALID_SOCKET;
        }
        {
            std::lock_guard<std::mutex> lock(clients_mtx_);
            for (SOCKET s : client_sockets_) {
                if (s != INVALID_SOCKET) closesocket(s);
            }
            client_sockets_.clear();
        }
        if (accept_thread_.joinable()) accept_thread_.join();
        if (send_thread_.joinable())   send_thread_.join();
        WSACleanup();
        log("遥测输出端口已停止");
    }

private:
    void accept_loop() {
        while (running_.load()) {
            sockaddr_in ca{};
            int len = sizeof(ca);
            SOCKET client = accept(listen_socket_,
                reinterpret_cast<sockaddr*>(&ca), &len);
            if (client == INVALID_SOCKET) break;

            std::lock_guard<std::mutex> lock(clients_mtx_);
            client_sockets_.push_back(client);
            log("遥测客户端已连接");
        }
    }

    void send_loop() {
        while (running_.load()) {
            Telemetry t = api_->read_telemetry();

            // 构造 JSON
            json::Object data;
            data["lat"]       = json::Value(t.lat);
            data["lon"]       = json::Value(t.lon);
            data["alt_m"]     = json::Value(t.alt_m);
            data["agl_m"]     = json::Value(t.agl_m);
            data["hdg_true"]  = json::Value(t.hdg_true);
            data["ias_kts"]   = json::Value(t.ias_kts);
            data["gs_kts"]    = json::Value(t.gs_kts);
            data["vs_fpm"]    = json::Value(t.vs_fpm);
            data["com1_freq"] = json::Value(t.com1_freq);
            data["com2_freq"] = json::Value(t.com2_freq);
            data["xpdr_code"] = json::Value(t.xpdr_code);
            data["xpdr_mode"] = json::Value(t.xpdr_mode);
            data["gear_pos"]  = json::Value(static_cast<double>(t.gear_pos));
            data["flaps_pos"] = json::Value(static_cast<double>(t.flaps_pos));
            data["on_ground"] = json::Value(t.on_ground);
            data["model_icao"] = json::Value(t.model_icao);

            // 时间戳（Unix epoch 秒，带小数）
            auto now = std::chrono::system_clock::now();
            double ts = std::chrono::duration<double>(
                now.time_since_epoch()).count();

            json::Object msg;
            msg["type"]      = json::Value(std::string("afs4_telemetry"));
            msg["timestamp"] = json::Value(ts);
            msg["data"]      = json::Value(std::move(data));

            std::string line = json::Value(std::move(msg)).dump() + "\n";

            // 广播给所有客户端，移除断开的
            std::lock_guard<std::mutex> lock(clients_mtx_);
            std::vector<SOCKET> alive;
            for (SOCKET s : client_sockets_) {
                int rc = send(s, line.c_str(),
                              static_cast<int>(line.size()), 0);
                if (rc != SOCKET_ERROR) {
                    alive.push_back(s);
                } else {
                    closesocket(s);
                    log("遥测客户端断开");
                }
            }
            client_sockets_ = std::move(alive);

            // 20Hz
            Sleep(SEND_INTERVAL_MS);
        }
    }

    void log(const std::string& msg) {
        if (log_fn_) log_fn_(msg);
    }

    std::shared_ptr<IAFS4Api> api_;
    int port_;
    std::string host_;
    std::atomic<bool> running_;
    SOCKET listen_socket_;
    std::thread accept_thread_;
    std::thread send_thread_;
    std::mutex clients_mtx_;
    std::vector<SOCKET> client_sockets_;
    LogFn log_fn_;
};

}  // namespace afs4
