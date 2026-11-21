#include <txvalidator.hpp>
#include <memory>

// This handle and the upstream Rust handle are both confined to one Rust
// thread. Rust serializes their construction against all validation calls.
extern "C" void *scary_spend_create(bool regtest) noexcept {
    try {
        auto v = std::make_unique<bsv::CTxValidator>(regtest ? "regtest" : "main");
        std::string error;
        if (regtest && (!v->SetGenesisActivationHeight(1, &error) ||
                        !v->SetChronicleActivationHeight(2, &error))) return nullptr;
        return v.release();
    } catch (...) { return nullptr; }
}
extern "C" void scary_spend_destroy(void *handle) noexcept {
    delete static_cast<bsv::CTxValidator *>(handle);
}
extern "C" int scary_verify_spend(void *handle, const uint8_t *tx, size_t tx_length,
        uint32_t input, const uint8_t *script, size_t script_length,
        int64_t value, int32_t created, int32_t height) noexcept {
    try {
        auto verdict = static_cast<bsv::CTxValidator *>(handle)->VerifySpend(
            {tx, tx_length}, input, {script, script_length}, value, created, height, true);
        return verdict.domain;
    } catch (...) { return TX_ERR_DOMAIN_EXCEPTION; }
}
