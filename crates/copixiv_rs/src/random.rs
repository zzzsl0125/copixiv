use std::collections::BinaryHeap;

use rayon::prelude::*;
use roaring::RoaringBitmap;

/// SplitMix64 mixing function for stateless per-request random permutation.
#[inline]
pub fn splitmix64(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
    x = (x ^ (x >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    x ^ (x >> 31)
}

/// OS-seeded 64-bit random seed for per-session true random browsing.
pub fn fresh_seed() -> u64 {
    let mut buf = [0u8; 8];
    if getrandom::getrandom(&mut buf).is_ok() {
        return u64::from_le_bytes(buf);
    }
    0x517C_C1B7_2722_0A95
}

pub fn hex16_encode(v: u64) -> String {
    format!("{v:016x}")
}

pub fn hex16_decode(s: &str) -> Result<u64, String> {
    if s.len() != 16 {
        return Err(format!("Expected 16-char hex u64, got len={}", s.len()));
    }
    u64::from_str_radix(s, 16).map_err(|e| format!("Invalid hex u64 '{s}': {e}"))
}

#[inline]
fn push_bounded(
    heap: &mut BinaryHeap<(u64, i64, u32)>,
    limit: usize,
    item: (u64, i64, u32),
) {
    if heap.len() <= limit {
        heap.push(item);
    } else if let Some(&worst) = heap.peek() {
        if item < worst {
            heap.pop();
            heap.push(item);
        }
    }
}

#[inline]
fn eval_candidate(
    doc: u32,
    novel_id: i64,
    seed: u64,
    cursor: Option<(u64, i64)>,
    limit: usize,
    heap: &mut BinaryHeap<(u64, i64, u32)>,
) {
    let h = splitmix64((novel_id as u64) ^ seed);
    if let Some((ch, cid)) = cursor {
        if (h, novel_id) <= (ch, cid) {
            return;
        }
    }
    push_bounded(heap, limit, (h, novel_id, doc));
}

/// Select the smallest `limit` `(hash, novel_id, doc)` tuples from `bitmap`
/// (optionally gated by `pred`) without materializing/sorting the full corpus.
pub fn top_k_random_bitmap<F>(
    bitmap: &RoaringBitmap,
    doc_ids: &[i64],
    seed: u64,
    cursor: Option<(u64, i64)>,
    limit: usize,
    mut pred: F,
) -> (Vec<(u64, i64, u32)>, bool)
where
    F: FnMut(u32) -> bool,
{
    if limit == 0 {
        // Still detect whether at least one candidate exists after cursor.
        for doc in bitmap.iter() {
            if !pred(doc) {
                continue;
            }
            let nid = doc_ids[doc as usize];
            let h = splitmix64((nid as u64) ^ seed);
            if cursor.map_or(true, |(ch, cid)| (h, nid) > (ch, cid)) {
                return (Vec::new(), true);
            }
        }
        return (Vec::new(), false);
    }

    let mut heap: BinaryHeap<(u64, i64, u32)> = BinaryHeap::with_capacity(limit + 1);
    for doc in bitmap.iter() {
        if !pred(doc) {
            continue;
        }
        eval_candidate(doc, doc_ids[doc as usize], seed, cursor, limit, &mut heap);
    }

    let has_more = heap.len() > limit;
    let mut sorted = heap.into_sorted_vec();
    sorted.truncate(limit);
    (sorted, has_more)
}

/// Parallel fused chunked Top-K over dense slot space `0..total_slots`.
/// Handles both unfiltered large corpora and expensive inline predicates
/// (such as 1-byte keyword substring scans) in a single lock-free pass.
pub fn top_k_random_parallel<F>(
    total_slots: u32,
    doc_ids: &[i64],
    seed: u64,
    cursor: Option<(u64, i64)>,
    limit: usize,
    pred: F,
) -> (Vec<(u64, i64, u32)>, bool)
where
    F: Fn(u32) -> bool + Sync,
{
    const CHUNK: u32 = 4096;
    if limit == 0 || total_slots == 0 {
        return (Vec::new(), false);
    }
    let n_chunks = total_slots.div_ceil(CHUNK);
    let merged = (0..n_chunks)
        .into_par_iter()
        .map(|chunk_idx| {
            let start = chunk_idx * CHUNK;
            let end = (start + CHUNK).min(total_slots);
            let mut heap: BinaryHeap<(u64, i64, u32)> = BinaryHeap::with_capacity(limit + 1);
            for doc in start..end {
                if !pred(doc) {
                    continue;
                }
                eval_candidate(doc, doc_ids[doc as usize], seed, cursor, limit, &mut heap);
            }
            heap
        })
        .reduce(
            || BinaryHeap::with_capacity(limit + 1),
            |mut acc, other| {
                for item in other {
                    push_bounded(&mut acc, limit, item);
                }
                acc
            },
        );

    let has_more = merged.len() > limit;
    let mut sorted = merged.into_sorted_vec();
    sorted.truncate(limit);
    (sorted, has_more)
}

/// Generic bounded top-K helper for unindexed string sort axes (`title`, `author_name`, `series_name`).
/// Keeps `limit + 1` earliest entries according to `item` `Ord`, using `Reverse` on outer heap?
/// NOTE: callers supply an `Ord` type where **smaller = earlier in output**, so `BinaryHeap`
/// keeps the `limit + 1` smallest items (worst of the kept items sits at `.peek()`).
pub fn collect_top_k<T: Ord, I: Iterator<Item = T>>(iter: I, limit: usize) -> (Vec<T>, bool) {
    if limit == 0 {
        let mut iter = iter;
        return (Vec::new(), iter.next().is_some());
    }
    let mut heap: BinaryHeap<T> = BinaryHeap::with_capacity(limit + 1);
    for item in iter {
        if heap.len() <= limit {
            heap.push(item);
        } else if let Some(worst) = heap.peek() {
            if item < *worst {
                heap.pop();
                heap.push(item);
            }
        }
    }
    let has_more = heap.len() > limit;
    let mut out = heap.into_sorted_vec();
    out.truncate(limit);
    (out, has_more)
}
