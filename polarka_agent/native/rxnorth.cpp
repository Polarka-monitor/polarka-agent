// The proof of work of TrueNorth (RandomX with the chain's salt and the V2 programs) behind a plain C
// interface, so that a program in any language can check a header or look for a nonce.
#include <atomic>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>
#include "randomx.h"

namespace {
std::mutex g_life;
randomx_cache* g_cache = nullptr;
randomx_dataset* g_dataset = nullptr;
randomx_flags g_flags = RANDOMX_FLAG_DEFAULT;

void release_all() {
    if (g_dataset) { randomx_release_dataset(g_dataset); g_dataset = nullptr; }
    if (g_cache) { randomx_release_cache(g_cache); g_cache = nullptr; }
}

// hash and target as 256-bit numbers, least significant byte first
bool at_or_below(const unsigned char* hash, const unsigned char* target) {
    for (int i = 31; i >= 0; i--) {
        if (hash[i] < target[i]) return true;
        if (hash[i] > target[i]) return false;
    }
    return true;
}
}  // namespace

extern "C" {

// key: 32 bytes (the seed block's hash in the byte order of the header; zeros before the first epoch).
// fast != 0 builds the 2 GiB dataset with `threads` threads. Returns 0, or a negative number.
int rxn_prepare(const unsigned char* key, int fast, int threads) {
    std::lock_guard<std::mutex> life(g_life);
    release_all();
    g_flags = randomx_get_flags() | RANDOMX_FLAG_V2;
    g_cache = randomx_alloc_cache(g_flags);
    if (!g_cache) return -1;
    randomx_init_cache(g_cache, key, 32);
    if (!fast) return 0;
    g_dataset = randomx_alloc_dataset(RANDOMX_FLAG_DEFAULT);
    if (!g_dataset) return -2;
    const unsigned long items = randomx_dataset_item_count();
    if (threads < 1) threads = 1;
    std::vector<std::thread> init;
    for (int i = 0; i < threads; i++) {
        const unsigned long start = items / threads * i;
        const unsigned long count = (i == threads - 1) ? items - start : items / threads;
        init.emplace_back([start, count] { randomx_init_dataset(g_dataset, g_cache, start, count); });
    }
    for (auto& t : init) t.join();
    return 0;
}

int rxn_flags(void) { return static_cast<int>(g_flags); }

// One machine per thread that hashes. Null when nothing is prepared.
void* rxn_vm_new(void) {
    if (!g_cache) return nullptr;
    randomx_flags flags = g_flags;
    if (g_dataset) flags |= RANDOMX_FLAG_FULL_MEM;
    return randomx_create_vm(flags, g_cache, g_dataset);
}

void rxn_vm_free(void* vm) { if (vm) randomx_destroy_vm(static_cast<randomx_vm*>(vm)); }

// The proof-of-work hash of an 80-byte header.
void rxn_hash(void* vm, const unsigned char* header80, unsigned char* out32) {
    randomx_calculate_hash(static_cast<randomx_vm*>(vm), header80, 80, out32);
}

// Tries the nonces start, start + step, ... `count` times with the 76 bytes before the nonce.
// Returns 1 with the nonce and its hash when one is at or below the target, else 0.
int rxn_scan(void* vm, const unsigned char* header76, const unsigned char* target32, uint32_t start, uint32_t step,
             uint64_t count, uint32_t* nonce, unsigned char* out32) {
    unsigned char in[80], out[RANDOMX_HASH_SIZE];
    std::memcpy(in, header76, 76);
    uint32_t n = start;
    for (uint64_t i = 0; i < count; i++, n += step) {
        std::memcpy(in + 76, &n, 4);
        randomx_calculate_hash(static_cast<randomx_vm*>(vm), in, sizeof(in), out);
        if (at_or_below(out, target32)) {
            *nonce = n;
            std::memcpy(out32, out, 32);
            return 1;
        }
    }
    return 0;
}

void rxn_release(void) { std::lock_guard<std::mutex> life(g_life); release_all(); }

}  // extern "C"
