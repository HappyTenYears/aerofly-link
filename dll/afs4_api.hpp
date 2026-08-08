/**
 * afs4_api.hpp —— AFS4 内部 API 抽象层
 * ================================================================
 * 封装 Aerofly FS 4 的应答机/无线电写入接口。
 *
 * 实际 AFS4 DLL API 签名需参照官方 External DLL Sample 项目，
 * 此处以 TODO 标注待补充部分。提供 Mock 模式用于无 AFS4 环境下的开发测试。
 */
#pragma once

#include <string>
#include <mutex>
#include <atomic>
#include <cstdint>

namespace afs4 {

// ── 写入结果 ──────────────────────────────────────────────────────
enum class WriteStatus {
    OK,             // 写入成功
    UNSUPPORTED,    // AFS4 不支持此操作
    ERR,            // 写入出错（避免与 windows.h 的 ERROR 宏冲突）
};

inline const char* status_str(WriteStatus s) {
    switch (s) {
        case WriteStatus::OK:          return "ok";
        case WriteStatus::UNSUPPORTED: return "unsupported";
        case WriteStatus::ERR:          return "error";
    }
    return "error";
}

// ── 遥测数据快照 ──────────────────────────────────────────────────
struct Telemetry {
    double lat       = 0.0;
    double lon       = 0.0;
    double alt_m     = 0.0;
    double agl_m     = 0.0;
    double hdg_true  = 0.0;
    double ias_kts   = 0.0;
    double gs_kts    = 0.0;
    double vs_fpm    = 0.0;
    int    com1_freq = 122800;   // kHz*10
    int    com2_freq = 119500;
    std::string xpdr_code = "7000";
    std::string xpdr_mode = "SBY";   // OFF/SBY/ON/ALT
    float  gear_pos  = 0.0f;
    float  flaps_pos = 0.0f;
    bool   on_ground = true;
    std::string model_icao = "B738";
};

/**
 * AFS4 API 接口
 *
 * 实际实现需调用 AFS4 External DLL SDK 提供的函数。
 * TODO: 根据 AFS4 官方 DLL Sample 补充以下方法的实际 API 调用。
 */
class IAFS4Api {
public:
    virtual ~IAFS4Api() = default;

    // ── 读取遥测 ──
    virtual Telemetry read_telemetry() = 0;

    // ── 应答机控制 ──
    /// 设置应答机代码（4 位八进制）
    /// @return WriteStatus::OK / UNSUPPORTED / ERROR
    virtual WriteStatus set_xpdr_code(const std::string& code) = 0;

    /// 设置应答机模式（OFF/SBY/ON/ALT）
    /// @return WriteStatus::OK / UNSUPPORTED / ERROR
    virtual WriteStatus set_xpdr_mode(const std::string& mode) = 0;

    /// 触发 IDENT 信号
    /// @param duration_s 持续秒数
    virtual WriteStatus trigger_ident(int duration_s) = 0;

    // ── 无线电控制 ──
    virtual WriteStatus set_com1_freq(int freq_khz_x10) = 0;
    virtual WriteStatus set_com2_freq(int freq_khz_x10) = 0;

    /// 是否处于模拟模式（无真实 AFS4 连接）
    virtual bool is_mock() const = 0;
};

// ════════════════════════════════════════════════════════════════════
//  MockAFS4Api —— 模拟实现（无 AFS4 环境下开发测试用）
// ════════════════════════════════════════════════════════════════════

class MockAFS4Api : public IAFS4Api {
public:
    /// @param support_xpdr_mode_write 模拟 AFS4 是否支持外部写入应答机模式
    /// @param support_xpdr_code_write 模拟是否支持写入 Squawk Code
    MockAFS4Api(bool support_xpdr_mode_write = true,
                bool support_xpdr_code_write = true)
        : mode_write_ok_(support_xpdr_mode_write)
        , code_write_ok_(support_xpdr_code_write)
    {
        // 初始模拟飞行状态：北京区域巡航
        tel_.lat = 39.9042;
        tel_.lon = 116.4074;
        tel_.alt_m = 1200.0;
        tel_.hdg_true = 270.0;
        tel_.gs_kts = 145.0;
        tel_.ias_kts = 145.0;
        tel_.on_ground = false;
    }

    Telemetry read_telemetry() override {
        std::lock_guard<std::mutex> lock(mtx_);
        // 模拟轻微位置变化
        tel_.lat += 0.0001;
        tel_.lon += 0.0001;
        return tel_;
    }

    WriteStatus set_xpdr_code(const std::string& code) override {
        std::lock_guard<std::mutex> lock(mtx_);
        if (!code_write_ok_) return WriteStatus::UNSUPPORTED;
        tel_.xpdr_code = code;
        return WriteStatus::OK;
    }

    WriteStatus set_xpdr_mode(const std::string& mode) override {
        std::lock_guard<std::mutex> lock(mtx_);
        if (!mode_write_ok_) return WriteStatus::UNSUPPORTED;
        tel_.xpdr_mode = mode;
        return WriteStatus::OK;
    }

    WriteStatus trigger_ident(int /*duration_s*/) override {
        // Mock 模式下 IDENT 总是支持
        return WriteStatus::OK;
    }

    WriteStatus set_com1_freq(int freq) override {
        std::lock_guard<std::mutex> lock(mtx_);
        tel_.com1_freq = freq;
        return WriteStatus::OK;
    }

    WriteStatus set_com2_freq(int freq) override {
        std::lock_guard<std::mutex> lock(mtx_);
        tel_.com2_freq = freq;
        return WriteStatus::OK;
    }

    bool is_mock() const override { return true; }

private:
    std::mutex mtx_;
    Telemetry tel_;
    bool mode_write_ok_;
    bool code_write_ok_;
};

// ════════════════════════════════════════════════════════════════════
//  RealAFS4Api —— 真实 AFS4 API 实现（骨架，待补充）
// ════════════════════════════════════════════════════════════════════
//
// TODO: 基于 AFS4 External DLL Sample 项目，补充以下方法的实际实现。
//       AFS4 SDK 通常通过全局接口指针或导出函数访问模拟器内部状态。
//       参考资源：
//       - GitHub 搜索 "Aerofly FS 4 External DLL"
//       - 社区项目 "Aerofly FS 4 ↔ Python Real-Time Bridge"
//
// class RealAFS4Api : public IAFS4Api {
//     Telemetry read_telemetry() override {
//         Telemetry t;
//         // TODO: 调用 AFS4 SDK 读取飞行数据
//         //   t.lat = afs4_get_latitude();
//         //   t.lon = afs4_get_longitude();
//         //   ...
//         return t;
//     }
//     WriteStatus set_xpdr_code(const std::string& code) override {
//         // TODO: 调用 AFS4 SDK 写入应答机代码
//         //   int result = afs4_set_transponder_code(code.c_str());
//         //   if (result == AFS4_OK) return WriteStatus::OK;
//         //   if (result == AFS4_UNSUPPORTED) return WriteStatus::UNSUPPORTED;
//         //   return WriteStatus::ERR;
//         return WriteStatus::UNSUPPORTED;
//     }
//     WriteStatus set_xpdr_mode(const std::string& mode) override {
//         // TODO: 调用 AFS4 SDK 写入应答机模式
//         //   注意：set_xpdr_mode 是否被 AFS4 接受取决于具体机型和 DLL 版本。
//         //   如果 AFS4 不支持写入模式，应返回 UNSUPPORTED，
//         //   客户端将自动降级到"虚拟模式"方案。
//         return WriteStatus::UNSUPPORTED;
//     }
//     // ... 其他方法
// };

}  // namespace afs4
