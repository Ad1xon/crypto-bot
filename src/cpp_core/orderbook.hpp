// crypto_core: fixed-size L2 order book that also tracks CVD and OFI.
//
// No pybind11 / Python includes in here on purpose, so the book can be tested
// or benchmarked from plain C++. The Python wrapper is in bindings.cpp.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace crypto_core {

/// Max price levels per side. Hyperliquid's `l2Book` channel always sends 20,
/// so everything is sized at compile time and we never allocate on updates.
inline constexpr std::size_t kMaxDepth = 20;

/// Milliseconds per day, used to reset CVD at midnight UTC.
inline constexpr std::int64_t kMsPerDay = 86'400'000;

/// One price level (16 bytes, plain struct so copying is cheap).
struct Level {
    double price;
    double size;
};

/**
 * @brief L2 book built from full snapshots. Updates CVD and OFI as data comes in.
 *
 * How it works
 * ------------
 * - **No heap allocations**: each side is a `std::array<Level, kMaxDepth>`, so
 *   the whole object is ~1.6 KiB and fits in L1 cache.
 * - **Two buffers per side**: a new snapshot is written into the unused buffer
 *   and then we flip the index. That way the previous book (needed for OFI) is
 *   still there and we don't have to copy it.
 * - **OFI** (order flow imbalance) is computed for every level. For level `m`
 *   between the previous snapshot (n-1) and the new one (n):
 *
 *       e_m =  1{Pb_n >= Pb_{n-1}} * qb_n  -  1{Pb_n <= Pb_{n-1}} * qb_{n-1}
 *            - 1{Pa_n <= Pa_{n-1}} * qa_n  +  1{Pa_n >= Pa_{n-1}} * qa_{n-1}
 *
 *   If a level is missing in one of the snapshots we treat it as price 0
 *   (bids) or +inf (asks) with size 0. Then a level that appears or disappears
 *   just adds its size with the right sign.
 * - **CVD** is the running sum of aggressor volume (buys +, sells -). With
 *   `auto_reset_cvd` on, it goes back to 0 on the first trade of a new UTC day,
 *   same as `HyperliquidStreamer._maybe_reset_cvd`. The stored day only moves
 *   forward, so a late trade from yesterday can't cause a second reset.
 *
 * Assumes bids are sorted high to low and asks low to high (that's how
 * Hyperliquid sends them). We don't re-check the order on every update.
 *
 * Not thread-safe. Use one instance per symbol per thread.
 */
class OrderBook {
public:
    /**
     * @param depth           Levels per side to keep, 1..kMaxDepth.
     * @param auto_reset_cvd  Reset CVD on the first trade of every UTC day.
     * @throws std::invalid_argument if `depth` is out of range.
     */
    explicit OrderBook(std::size_t depth = kMaxDepth, bool auto_reset_cvd = true);

    // ------------------------------------------------------------------ ingest

    /**
     * @brief Replace both sides with a new snapshot and recompute OFI.
     *
     * @param bids    Flat `[price, size]` pairs, best bid first.
     * @param n_bids  Number of pairs in `bids`.
     * @param asks    Flat `[price, size]` pairs, best ask first.
     * @param n_asks  Number of pairs in `asks`.
     * @param ts_ms   Exchange timestamp of the snapshot (ms since epoch).
     * @return Level-1 OFI for this update (0.0 for the first snapshot).
     *
     * Rows past `depth()` are ignored. Rows with NaN/inf or size <= 0 are
     * skipped, so they don't shift the level indices.
     */
    double update(const double* bids, std::size_t n_bids, const double* asks, std::size_t n_asks,
                  std::int64_t ts_ms) noexcept;

    /**
     * @brief Add one trade to CVD.
     *
     * @param price   Trade price (saved as `last_trade_price`).
     * @param size    Trade size; NaN/inf or negative sizes are ignored.
     * @param is_buy  True if the taker was buying.
     * @param ts_ms   Exchange timestamp (ms since epoch). If <= 0 we skip the
     *                daily reset check for this trade.
     * @return CVD after the trade.
     */
    double apply_trade(double price, double size, bool is_buy, std::int64_t ts_ms) noexcept;

    /// Set CVD to `value` and clear the buy/sell volume counters.
    void reset_cvd(double value = 0.0) noexcept;

    /// Clear everything (book, OFI, CVD), e.g. after the WebSocket reconnects.
    void reset() noexcept;

    // ----------------------------------------------------------------- queries

    [[nodiscard]] std::size_t depth() const noexcept { return depth_; }
    [[nodiscard]] bool auto_reset_cvd() const noexcept { return auto_reset_cvd_; }

    [[nodiscard]] std::size_t bid_levels() const noexcept { return n_bids_[cur_]; }
    [[nodiscard]] std::size_t ask_levels() const noexcept { return n_asks_[cur_]; }
    [[nodiscard]] const Level* bids() const noexcept { return bids_[cur_].data(); }
    [[nodiscard]] const Level* asks() const noexcept { return asks_[cur_].data(); }

    [[nodiscard]] double best_bid_price() const noexcept { return bid_levels() ? bids()[0].price : 0.0; }
    [[nodiscard]] double best_bid_size() const noexcept { return bid_levels() ? bids()[0].size : 0.0; }
    [[nodiscard]] double best_ask_price() const noexcept { return ask_levels() ? asks()[0].price : 0.0; }
    [[nodiscard]] double best_ask_size() const noexcept { return ask_levels() ? asks()[0].size : 0.0; }

    /// True if there is at least one bid and one ask.
    [[nodiscard]] bool is_two_sided() const noexcept { return bid_levels() != 0 && ask_levels() != 0; }

    /// (best_bid + best_ask) / 2, or 0.0 if one side is empty.
    [[nodiscard]] double mid_price() const noexcept;

    /// best_ask - best_bid, or 0.0 if one side is empty.
    [[nodiscard]] double spread() const noexcept;

    /// Mid price weighted by the top-of-book sizes, or 0.0 if one side is empty.
    [[nodiscard]] double microprice() const noexcept;

    /// best_bid_size - best_ask_size. The old Python streamer logged this as
    /// "ofi", so we keep it to stay compatible with the existing signals.
    [[nodiscard]] double top_imbalance() const noexcept { return best_bid_size() - best_ask_size(); }

    /// (bid volume - ask volume) / (bid volume + ask volume) over the first
    /// `levels` levels (capped at depth). Between -1 and 1; 0.0 if empty.
    [[nodiscard]] double depth_imbalance(std::size_t levels) const noexcept;

    /// Level-1 OFI from the last update.
    [[nodiscard]] double ofi() const noexcept { return ofi_levels_[0]; }

    /// Sum of OFI over the first `levels` levels (capped at depth).
    [[nodiscard]] double multi_level_ofi(std::size_t levels) const noexcept;

    /// OFI per level from the last update; the first `depth()` entries are valid.
    [[nodiscard]] const double* ofi_levels() const noexcept { return ofi_levels_.data(); }

    /// Running total of level-1 OFI since construction / reset().
    [[nodiscard]] double cumulative_ofi() const noexcept { return cumulative_ofi_; }

    [[nodiscard]] double cvd() const noexcept { return cvd_; }
    [[nodiscard]] double buy_volume() const noexcept { return buy_volume_; }
    [[nodiscard]] double sell_volume() const noexcept { return sell_volume_; }
    [[nodiscard]] double last_trade_price() const noexcept { return last_trade_price_; }

    [[nodiscard]] std::uint64_t update_count() const noexcept { return update_count_; }
    [[nodiscard]] std::uint64_t trade_count() const noexcept { return trade_count_; }
    [[nodiscard]] std::int64_t last_update_ms() const noexcept { return last_update_ms_; }
    [[nodiscard]] std::int64_t last_trade_ms() const noexcept { return last_trade_ms_; }

private:
    using Side = std::array<Level, kMaxDepth>;

    /// Copy the valid rows from a flat [price, size] buffer into `dst`.
    std::size_t load_side(const double* src, std::size_t n_rows, Side& dst) const noexcept;

    /// Fill `ofi_levels_` by comparing the previous (`prev`) and current (`cur_`) buffers.
    void compute_ofi(std::size_t prev) noexcept;

    std::size_t depth_;
    bool auto_reset_cvd_;

    // Two buffers per side: `cur_` is the live one, `cur_ ^ 1` holds the previous snapshot.
    std::array<Side, 2> bids_{};
    std::array<Side, 2> asks_{};
    std::array<std::size_t, 2> n_bids_{};
    std::array<std::size_t, 2> n_asks_{};
    std::size_t cur_ = 0;
    bool has_prev_ = false;

    std::array<double, kMaxDepth> ofi_levels_{};
    double cumulative_ofi_ = 0.0;

    double cvd_ = 0.0;
    double buy_volume_ = 0.0;
    double sell_volume_ = 0.0;
    double last_trade_price_ = 0.0;
    std::int64_t last_trade_day_ = -1;

    std::uint64_t update_count_ = 0;
    std::uint64_t trade_count_ = 0;
    std::int64_t last_update_ms_ = 0;
    std::int64_t last_trade_ms_ = 0;
};

}  // namespace crypto_core
