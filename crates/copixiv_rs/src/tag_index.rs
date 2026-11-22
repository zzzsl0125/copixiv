use std::hash::Hasher;
use std::mem::size_of;

use memchr::memmem::Finder;
use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet, FxHasher};

use crate::arena::PackedArena;
use crate::model::{TagCandidate, TagSuggestItem};
use crate::postings::{PostingHandle, PostingSlab, TitleBuckets};
use crate::text_index::{collect_query_char_bigrams, contains_ascii_ci};

const MAX_CANDIDATES: usize = 50;
const PREFIX_CANDIDATE_CAP: usize = 200;
const SCAN_FLOOR: usize = 20;
const SCAN_CAP: usize = 200;

#[inline]
fn hash_tag_name(name: &str) -> u64 {
    let mut hasher = FxHasher::default();
    hasher.write(name.as_bytes());
    hasher.finish()
}

/// In-process tag similarity index (~76k tags), replacing PostgreSQL `pg_bigm`
/// GIN scans for `/api/tag-aliases/suggest`.
#[derive(Default)]
pub struct TagIndex {
    ids: Vec<i64>,
    ref_counts: Vec<i32>,
    alive: Vec<bool>,
    name_arena: PackedArena,
    free_slots: Vec<u32>,
    id_to_slot: FxHashMap<i64, u32>,
    name_buckets: TitleBuckets,
    aliased_ids: FxHashSet<i64>,
    sorted_slots: Vec<u32>,
    pub sorted_dirty: bool,
    first_char_index: FxHashMap<char, Vec<u32>>,
    char_bigrams: FxHashMap<u64, PostingHandle>,
    bigram_slab: PostingSlab,
}

fn char_bigrams_of_lower(lower: &str, scratch: &mut Vec<u64>) {
    collect_query_char_bigrams(lower, scratch);
}

fn jaccard_permille(a_bgs: &[u64], b_bgs: &[u64], a_chars: usize, b_chars: usize) -> (u32, usize) {
    if a_bgs.is_empty() || b_bgs.is_empty() {
        let min_c = a_chars.min(b_chars);
        let max_c = a_chars.max(b_chars).max(1);
        return (((min_c * 1000) / max_c) as u32, 0);
    }
    let (mut i, mut j, mut inter) = (0usize, 0usize, 0usize);
    while i < a_bgs.len() && j < b_bgs.len() {
        match a_bgs[i].cmp(&b_bgs[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                inter += 1;
                i += 1;
                j += 1;
            }
        }
    }
    let union = a_bgs.len() + b_bgs.len() - inter;
    let sim = (inter * 1000).checked_div(union).unwrap_or(0) as u32;
    (sim, inter)
}

#[inline]
fn contains_raw_needle_ci(haystack_lower: &[u8], needle: &[u8]) -> bool {
    if needle.len() > haystack_lower.len() {
        return false;
    }
    if !needle.iter().any(|b| b.is_ascii_uppercase()) {
        return memchr::memmem::find(haystack_lower, needle).is_some();
    }
    if needle.len() <= 256 {
        let mut buf = [0u8; 256];
        let slice = &mut buf[..needle.len()];
        for (dst, &src) in slice.iter_mut().zip(needle.iter()) {
            *dst = src.to_ascii_lowercase();
        }
        memchr::memmem::find(haystack_lower, slice).is_some()
    } else {
        let lower: Vec<u8> = needle.iter().map(u8::to_ascii_lowercase).collect();
        memchr::memmem::find(haystack_lower, &lower).is_some()
    }
}

impl TagIndex {
    pub fn from_snapshot(tags: Vec<(i64, String, i32)>, aliased_ids: Vec<i64>) -> Self {
        let approx_bytes: usize = tags.iter().map(|(_, n, _)| n.len()).sum();
        let cap = tags.len();
        let mut idx = Self {
            ids: Vec::with_capacity(cap),
            ref_counts: Vec::with_capacity(cap),
            alive: Vec::with_capacity(cap),
            name_arena: PackedArena::with_capacity(cap, approx_bytes),
            free_slots: Vec::new(),
            id_to_slot: FxHashMap::with_capacity_and_hasher(cap, Default::default()),
            name_buckets: TitleBuckets::with_capacity(cap),
            aliased_ids: aliased_ids.into_iter().collect(),
            sorted_slots: Vec::with_capacity(cap),
            sorted_dirty: true,
            first_char_index: FxHashMap::default(),
            char_bigrams: FxHashMap::default(),
            bigram_slab: PostingSlab::new(),
        };

        let mut bg_scratch = Vec::with_capacity(32);
        for (id, name, reference_count) in tags {
            if idx.id_to_slot.contains_key(&id) || idx.find_slot_by_name(&name).is_some() {
                continue;
            }
            let slot = idx.ids.len() as u32;
            idx.ids.push(id);
            idx.ref_counts.push(reference_count);
            idx.alive.push(true);
            idx.name_arena.set_str(slot, &name);

            idx.id_to_slot.insert(id, slot);
            idx.name_buckets.insert(hash_tag_name(&name), slot);

            char_bigrams_of_lower(&name, &mut bg_scratch);
            idx.insert_bigrams_from_slice(slot, &bg_scratch);
        }

        idx.ensure_sorted();
        idx.compact_and_shrink();
        idx
    }

    fn compact_and_shrink(&mut self) {
        self.ids.shrink_to_fit();
        self.ref_counts.shrink_to_fit();
        self.alive.shrink_to_fit();
        self.name_arena.shrink_to_fit();
        self.id_to_slot.shrink_to_fit();
        self.name_buckets.shrink_to_fit();
        self.sorted_slots.shrink_to_fit();
        for v in self.first_char_index.values_mut() {
            v.shrink_to_fit();
        }
        self.first_char_index.shrink_to_fit();
        self.bigram_slab.compact(self.char_bigrams.values_mut());
        self.char_bigrams.shrink_to_fit();
    }

    #[inline]
    fn find_slot_by_name(&self, name: &str) -> Option<u32> {
        let hash = hash_tag_name(name);
        let bucket = self.name_buckets.get(hash)?;
        bucket
            .as_slice()
            .iter()
            .find(|&&slot| self.alive[slot as usize] && self.name_arena.get_str(slot) == name)
            .copied()
    }

    #[inline]
    fn insert_bigrams_from_slice(&mut self, slot: u32, bigrams: &[u64]) {
        for &bg in bigrams {
            let h = self.char_bigrams.entry(bg).or_default();
            self.bigram_slab.insert(h, slot);
        }
    }

    #[inline]
    fn remove_bigrams_from_slice(&mut self, slot: u32, bigrams: &[u64]) {
        for &bg in bigrams {
            if let Some(h) = self.char_bigrams.get_mut(&bg) {
                self.bigram_slab.remove(h, slot);
                if h.is_empty() {
                    self.char_bigrams.remove(&bg);
                }
            }
        }
    }

    pub fn build_sorted_state(&self) -> (Vec<u32>, FxHashMap<char, Vec<u32>>) {
        let mut sorted_slots = Vec::with_capacity(self.id_to_slot.len());
        for (slot_idx, &is_alive) in self.alive.iter().enumerate() {
            if is_alive {
                sorted_slots.push(slot_idx as u32);
            }
        }
        let ids = &self.ids;
        let ref_counts = &self.ref_counts;
        sorted_slots.par_sort_unstable_by(|&a, &b| {
            let ua = a as usize;
            let ub = b as usize;
            ref_counts[ub]
                .cmp(&ref_counts[ua])
                .then_with(|| ids[ub].cmp(&ids[ua]))
        });

        let mut first_char_index: FxHashMap<char, Vec<u32>> = FxHashMap::default();
        for &slot in &sorted_slots {
            if let Some(ch) = self.name_arena.get_str(slot).chars().next() {
                first_char_index
                    .entry(ch.to_ascii_lowercase())
                    .or_default()
                    .push(slot);
            }
        }
        (sorted_slots, first_char_index)
    }

    pub fn install_sorted_state(
        &mut self,
        sorted_slots: Vec<u32>,
        first_char_index: FxHashMap<char, Vec<u32>>,
    ) {
        self.sorted_slots = sorted_slots;
        self.first_char_index = first_char_index;
        self.sorted_dirty = false;
    }

    pub fn ensure_sorted(&mut self) {
        if !self.sorted_dirty {
            return;
        }
        let (sorted_slots, first_char_index) = self.build_sorted_state();
        self.install_sorted_state(sorted_slots, first_char_index);
    }

    pub fn apply_delta(
        &mut self,
        delete_tag_names: Option<Vec<String>>,
        upsert_tags: Option<Vec<(i64, String, i32)>>,
        aliased_tag_ids: Option<Vec<i64>>,
    ) {
        if let Some(ids) = aliased_tag_ids {
            self.aliased_ids = ids.into_iter().collect();
        }

        let mut scratch = Vec::with_capacity(32);

        if let Some(del_names) = delete_tag_names {
            for name in del_names {
                let Some(slot) = self.find_slot_by_name(&name) else {
                    continue;
                };
                let idx = slot as usize;
                if !self.alive[idx] {
                    continue;
                }
                self.name_buckets.remove(hash_tag_name(&name), slot);
                self.id_to_slot.remove(&self.ids[idx]);
                char_bigrams_of_lower(self.name_arena.get_str(slot), &mut scratch);
                self.remove_bigrams_from_slice(slot, &scratch);
                self.name_arena.clear_slot(slot);
                self.alive[idx] = false;
                self.ref_counts[idx] = 0;
                self.free_slots.push(slot);
                self.sorted_dirty = true;
            }
        }

        if let Some(upserts) = upsert_tags {
            for (tid, name, rc) in upserts {
                if let Some(slot) = self.find_slot_by_name(&name) {
                    let idx = slot as usize;
                    if self.ids[idx] != tid {
                        self.id_to_slot.remove(&self.ids[idx]);
                        self.ids[idx] = tid;
                        self.id_to_slot.insert(tid, slot);
                    }
                    if self.ref_counts[idx] != rc {
                        self.ref_counts[idx] = rc;
                        self.sorted_dirty = true;
                    }
                    continue;
                }

                if let Some(&slot) = self.id_to_slot.get(&tid) {
                    let idx = slot as usize;
                    let old_hash = hash_tag_name(self.name_arena.get_str(slot));
                    self.name_buckets.remove(old_hash, slot);
                    char_bigrams_of_lower(self.name_arena.get_str(slot), &mut scratch);
                    self.remove_bigrams_from_slice(slot, &scratch);

                    self.name_arena.set_str(slot, &name);
                    self.ref_counts[idx] = rc;
                    self.name_buckets.insert(hash_tag_name(&name), slot);
                    char_bigrams_of_lower(&name, &mut scratch);
                    self.insert_bigrams_from_slice(slot, &scratch);
                    self.sorted_dirty = true;
                    continue;
                }

                let slot = if let Some(reused) = self.free_slots.pop() {
                    let idx = reused as usize;
                    self.ids[idx] = tid;
                    self.ref_counts[idx] = rc;
                    self.alive[idx] = true;
                    reused
                } else {
                    let s = self.ids.len() as u32;
                    self.ids.push(tid);
                    self.ref_counts.push(rc);
                    self.alive.push(true);
                    s
                };
                self.name_arena.set_str(slot, &name);
                self.id_to_slot.insert(tid, slot);
                self.name_buckets.insert(hash_tag_name(&name), slot);
                char_bigrams_of_lower(&name, &mut scratch);
                self.insert_bigrams_from_slice(slot, &scratch);
                self.sorted_dirty = true;
            }
        }

        self.name_arena.maybe_compact();
        self.bigram_slab
            .maybe_compact(self.char_bigrams.values_mut());
    }

    pub fn tag_count(&self) -> usize {
        self.id_to_slot.len()
    }

    pub fn approx_bytes(&self) -> usize {
        let vec_bytes = self.ids.capacity() * size_of::<i64>()
            + self.ref_counts.capacity() * size_of::<i32>()
            + self.alive.capacity() * size_of::<bool>()
            + self.free_slots.capacity() * size_of::<u32>()
            + self.sorted_slots.capacity() * size_of::<u32>();
        let arena_bytes = self.name_arena.data.capacity()
            + self.name_arena.slots.capacity() * size_of::<(u32, u32)>();
        let map_bytes = self.id_to_slot.capacity() * (size_of::<i64>() + size_of::<u32>() + 4)
            + self.name_buckets.approx_heap_bytes()
            + self.aliased_ids.capacity() * (size_of::<i64>() + 4)
            + self.first_char_index.capacity() * (size_of::<char>() + size_of::<Vec<u32>>())
            + self
                .first_char_index
                .values()
                .map(|v| v.capacity() * size_of::<u32>())
                .sum::<usize>()
            + self.char_bigrams.capacity() * (size_of::<u64>() + size_of::<PostingHandle>() + 1)
            + self.bigram_slab.approx_heap_bytes();
        vec_bytes + arena_bytes + map_bytes
    }

    #[inline]
    fn to_dto(&self, slot: u32) -> TagCandidate {
        let idx = slot as usize;
        TagCandidate {
            id: self.ids[idx],
            name: self.name_arena.get_str(slot).to_owned(),
            reference_count: self.ref_counts[idx],
        }
    }

    fn suggest_for_slot(&self, target_slot: u32) -> Vec<TagCandidate> {
        let tgt_name = self.name_arena.get_str(target_slot);
        if tgt_name.is_empty() {
            return Vec::new();
        }
        let tgt_lower = tgt_name.to_ascii_lowercase();
        let tgt_chars = tgt_lower.chars().count();
        let mut tgt_bigrams = Vec::with_capacity(tgt_chars);
        char_bigrams_of_lower(&tgt_lower, &mut tgt_bigrams);

        // slot -> highest recall tier (3: superstring, 2: substring/jaccard>=0.5, 1: prefix)
        let mut matched: FxHashMap<u32, u8> = FxHashMap::default();
        let need_lower = tgt_lower.bytes().any(|b| b.is_ascii_lowercase());
        let finder = Finder::new(tgt_lower.as_bytes());

        // 1. Exact superstring recall: `cand_lower.contains(tgt_lower)`.
        if !tgt_bigrams.is_empty() {
            let mut ranked_bgs: Vec<(usize, PostingHandle)> = Vec::with_capacity(tgt_bigrams.len());
            let mut missing_bg = false;
            for &bg in &tgt_bigrams {
                let Some(handle) = self.char_bigrams.get(&bg).copied() else {
                    missing_bg = true;
                    break;
                };
                let len = handle.len(&self.bigram_slab);
                if len == 0 {
                    missing_bg = true;
                    break;
                }
                ranked_bgs.push((len, handle));
            }
            if !missing_bg {
                ranked_bgs.sort_unstable_by_key(|(len, _)| *len);
                let mut cand_bm = ranked_bgs[0].1.to_bitmap(&self.bigram_slab);
                for (i, &(_, handle)) in ranked_bgs.iter().enumerate().skip(1) {
                    if i >= 6 || cand_bm.len() <= 64 {
                        break;
                    }
                    if !handle.intersect_bitmap(&self.bigram_slab, &mut cand_bm) {
                        break;
                    }
                }
                for slot in cand_bm.iter() {
                    if slot == target_slot {
                        continue;
                    }
                    let c_idx = slot as usize;
                    if !self.alive[c_idx]
                        || self.ref_counts[c_idx] <= 0
                        || self.aliased_ids.contains(&self.ids[c_idx])
                    {
                        continue;
                    }
                    if contains_ascii_ci(
                        self.name_arena.get_bytes(slot),
                        tgt_lower.len(),
                        &finder,
                        need_lower,
                    ) {
                        matched.insert(slot, 3);
                    }
                }
            }
        } else {
            // Single-char or whitespace-only target tag: scan sorted slots directly.
            for &slot in &self.sorted_slots {
                if slot == target_slot {
                    continue;
                }
                let c_idx = slot as usize;
                if !self.alive[c_idx] || self.ref_counts[c_idx] <= 0 {
                    continue;
                }
                if self.aliased_ids.contains(&self.ids[c_idx]) {
                    continue;
                }
                if contains_ascii_ci(
                    self.name_arena.get_bytes(slot),
                    tgt_lower.len(),
                    &finder,
                    need_lower,
                ) {
                    matched.insert(slot, 3);
                }
            }
        }

        // 2. High bigram Jaccard similarity (>= 0.5) & reverse containment (`tgt.contains(cand)`).
        let mut sim_cache: FxHashMap<u32, u32> = FxHashMap::default();
        if tgt_bigrams.len() >= 2 {
            let mut by_len: Vec<(usize, PostingHandle)> = tgt_bigrams
                .iter()
                .filter_map(|&bg| {
                    self.char_bigrams
                        .get(&bg)
                        .copied()
                        .map(|h| (h.len(&self.bigram_slab), h))
                })
                .collect();
            by_len.sort_unstable_by_key(|(len, _)| *len);
            let probe_count = ((tgt_bigrams.len() / 2) + 1).min(by_len.len());
            let mut probe_slots: FxHashSet<u32> = FxHashSet::default();
            for &(_, handle) in &by_len[..probe_count] {
                for slot in handle.iter(&self.bigram_slab) {
                    if slot != target_slot && !matched.contains_key(&slot) {
                        probe_slots.insert(slot);
                    }
                }
            }
            let mut cand_bgs = Vec::with_capacity(32);
            for &slot in &probe_slots {
                let c_idx = slot as usize;
                let cand_name = self.name_arena.get_str(slot);
                if !self.alive[c_idx]
                    || self.ref_counts[c_idx] <= 0
                    || cand_name.is_empty()
                    || self.aliased_ids.contains(&self.ids[c_idx])
                {
                    continue;
                }
                let cand_chars = cand_name.chars().count();
                char_bigrams_of_lower(cand_name, &mut cand_bgs);
                let (sim, _) = jaccard_permille(&tgt_bigrams, &cand_bgs, tgt_chars, cand_chars);
                if sim >= 500
                    || (cand_chars >= 2
                        && contains_raw_needle_ci(tgt_lower.as_bytes(), cand_name.as_bytes()))
                {
                    sim_cache.insert(slot, sim);
                    matched
                        .entry(slot)
                        .and_modify(|t| *t = (*t).max(2))
                        .or_insert(2);
                }
            }
            // High-frequency bigrams skipped by `probe_count` can still match short
            // reverse-containment substrings (`tgt.contains(cand)`).
            for &(_, handle) in &by_len[probe_count..] {
                for slot in handle.iter(&self.bigram_slab) {
                    if slot == target_slot
                        || matched.contains_key(&slot)
                        || probe_slots.contains(&slot)
                    {
                        continue;
                    }
                    let c_idx = slot as usize;
                    let cand_name = self.name_arena.get_str(slot);
                    if !self.alive[c_idx]
                        || self.ref_counts[c_idx] <= 0
                        || cand_name.len() >= tgt_lower.len()
                        || self.aliased_ids.contains(&self.ids[c_idx])
                    {
                        continue;
                    }
                    if cand_name.chars().count() >= 2
                        && contains_raw_needle_ci(tgt_lower.as_bytes(), cand_name.as_bytes())
                    {
                        matched.insert(slot, 2);
                    }
                }
            }
        }

        // 3. First-char prefix matches (top eligible rows ordered by rc DESC, id DESC).
        if let Some(fc) = tgt_lower.chars().next() {
            if let Some(bucket) = self.first_char_index.get(&fc) {
                let mut taken = 0usize;
                for &slot in bucket {
                    if slot == target_slot {
                        continue;
                    }
                    let c_idx = slot as usize;
                    if !self.alive[c_idx]
                        || self.ref_counts[c_idx] <= 0
                        || self.aliased_ids.contains(&self.ids[c_idx])
                    {
                        continue;
                    }
                    matched.entry(slot).or_insert(1);
                    taken += 1;
                    if taken >= PREFIX_CANDIDATE_CAP {
                        break;
                    }
                }
            }
        }

        if matched.is_empty() {
            return Vec::new();
        }

        let mut cand_bgs = Vec::with_capacity(32);
        let mut scored: Vec<(u8, u32, i32, i64, u32)> = Vec::with_capacity(matched.len());
        for (slot, tier) in matched {
            let c_idx = slot as usize;
            let sim = if let Some(&cached) = sim_cache.get(&slot) {
                cached
            } else {
                let cand_name = self.name_arena.get_str(slot);
                char_bigrams_of_lower(cand_name, &mut cand_bgs);
                let (s, _) = jaccard_permille(
                    &tgt_bigrams,
                    &cand_bgs,
                    tgt_chars,
                    cand_name.chars().count(),
                );
                s
            };
            scored.push((tier, sim, self.ref_counts[c_idx], self.ids[c_idx], slot));
        }

        scored.sort_unstable_by(|a, b| {
            b.0.cmp(&a.0)
                .then_with(|| b.1.cmp(&a.1))
                .then_with(|| b.2.cmp(&a.2))
                .then_with(|| b.3.cmp(&a.3))
        });
        scored.truncate(MAX_CANDIDATES);

        scored
            .into_iter()
            .map(|(_, _, _, _, slot)| self.to_dto(slot))
            .collect()
    }

    pub fn suggest_aliases(
        &self,
        limit: usize,
        offset: i64,
        target_tag: Option<&str>,
    ) -> (Vec<TagSuggestItem>, Option<i64>) {
        debug_assert!(!self.sorted_dirty);

        if let Some(name) = target_tag.filter(|s| !s.is_empty()) {
            let Some(slot) = self.find_slot_by_name(name) else {
                return (Vec::new(), None);
            };
            let idx = slot as usize;
            if !self.alive[idx] || self.aliased_ids.contains(&self.ids[idx]) {
                return (Vec::new(), None);
            }
            let candidates = self.suggest_for_slot(slot);
            if candidates.is_empty() {
                return (Vec::new(), None);
            }
            return (
                vec![TagSuggestItem {
                    target: self.to_dto(slot),
                    candidates,
                }],
                None,
            );
        }

        if limit == 0 {
            return (Vec::new(), None);
        }

        let scan_limit = (limit * 4).clamp(SCAN_FLOOR, SCAN_CAP);
        let start_idx = if offset <= 0 {
            0usize
        } else {
            let Some(&cur_slot) = self.id_to_slot.get(&offset) else {
                return (Vec::new(), None);
            };
            let cur_idx = cur_slot as usize;
            if !self.alive[cur_idx] || self.ref_counts[cur_idx] <= 0 {
                // Option 2 zeroed the cursor tag's ref_count (or tag was deleted):
                // partition_point with cur_rc=0 would jump to the 0-rc tail and
                // truncate prematurely. Restart from 0 (already-aliased tags are skipped).
                0usize
            } else {
                let cur_rc = self.ref_counts[cur_idx];
                let cur_id = self.ids[cur_idx];
                self.sorted_slots.partition_point(|&slot| {
                    let s_idx = slot as usize;
                    (self.ref_counts[s_idx], self.ids[s_idx]) >= (cur_rc, cur_id)
                })
            }
        };

        let mut items: Vec<TagSuggestItem> = Vec::with_capacity(limit);
        let mut scanned = 0usize;
        let mut last_scanned_id: Option<i64> = None;

        for &slot in &self.sorted_slots[start_idx..] {
            let idx = slot as usize;
            if !self.alive[idx] || self.ref_counts[idx] <= 0 {
                break; // `sorted_slots` is descending by reference_count
            }
            let tid = self.ids[idx];
            if self.aliased_ids.contains(&tid) {
                continue;
            }
            scanned += 1;
            last_scanned_id = Some(tid);
            let candidates = self.suggest_for_slot(slot);
            if !candidates.is_empty() {
                items.push(TagSuggestItem {
                    target: self.to_dto(slot),
                    candidates,
                });
                if items.len() >= limit {
                    return (items, Some(tid));
                }
            }
            if scanned >= scan_limit {
                break;
            }
        }

        let next_cursor = if scanned >= scan_limit {
            last_scanned_id
        } else {
            None
        };
        (items, next_cursor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tag_index_suggests_substring_similarity_prefix_and_respects_aliases_and_special_chars() {
        let tags = vec![
            (1, "百合".into(), 100),
            (2, "百合漫画".into(), 50),
            (3, "真百合小説".into(), 10),
            (4, "日常".into(), 80),
            (5, "%高中生".into(), 40),
            (6, "%高中生女子".into(), 5),
            (7, "aliased_src".into(), 90),
            (8, "BanG_Dream!".into(), 60),
            (9, "BanG_Dream_Live".into(), 15),
        ];
        let mut idx = TagIndex::from_snapshot(tags, vec![7]);

        let (items, next_off) = idx.suggest_aliases(5, 0, Some("%高中生"));
        assert_eq!(next_off, None);
        assert_eq!(items.len(), 1);
        let cand_names: Vec<&str> = items[0]
            .candidates
            .iter()
            .map(|c| c.name.as_str())
            .collect();
        assert!(cand_names.contains(&"%高中生女子"));
        assert!(!cand_names.contains(&"日常"));

        assert!(idx.suggest_aliases(5, 0, Some("aliased_src")).0.is_empty());

        let (yuri_items, _) = idx.suggest_aliases(5, 0, Some("百合"));
        let yuri_cands: Vec<&str> = yuri_items[0]
            .candidates
            .iter()
            .map(|c| c.name.as_str())
            .collect();
        assert_eq!(yuri_cands[0], "百合漫画");
        assert!(yuri_cands.contains(&"真百合小説"));

        let (p1, cur1) = idx.suggest_aliases(2, 0, None);
        assert_eq!(p1.len(), 2);
        let cur1 = cur1.expect("first page should return a cursor");
        let (p2, cur2) = idx.suggest_aliases(10, cur1, None);
        assert!(!p2.is_empty());
        assert_eq!(cur2, None, "exhausted last batch must return None");
        let ids1: Vec<i64> = p1.iter().map(|i| i.target.id).collect();
        let ids2: Vec<i64> = p2.iter().map(|i| i.target.id).collect();
        for id in &ids2 {
            assert!(!ids1.contains(id));
        }

        assert_eq!(idx.suggest_aliases(5, 999999, None), (Vec::new(), None));

        // Simulate Option 2 zeroing the cursor tag's ref_count + marking it aliased:
        // scanning from cur1 must not jump to the 0-rc tail and truncate prematurely.
        let cursor_name = p1.last().unwrap().target.name.clone();
        idx.apply_delta(
            None,
            Some(vec![(cur1, cursor_name, 0)]),
            Some(vec![7, cur1]),
        );
        idx.ensure_sorted();
        let (after_zero, _) = idx.suggest_aliases(5, cur1, None);
        assert!(!after_zero.is_empty());
        assert!(after_zero.iter().all(|i| i.target.id != cur1));

        idx.apply_delta(
            Some(vec!["日常".into()]),
            Some(vec![(10, "百合小説".into(), 200)]),
            Some(vec![7]),
        );
        idx.ensure_sorted();
        let (after, _) = idx.suggest_aliases(5, 0, Some("百合"));
        assert!(after[0].candidates.iter().any(|c| c.name == "百合小説"));
    }

    #[test]
    fn tag_index_finds_high_freq_short_substring_on_long_target_and_ignores_single_cjk_char() {
        // Make bigram "ab" very high frequency compared to the rest of "abcdefghij"
        let mut tags: Vec<(i64, String, i32)> = vec![
            (1, "abcdefghij".into(), 500),
            (2, "ab".into(), 200),
            (3, "空".into(), 300),
            (4, "星空物語".into(), 400),
        ];
        for i in 10..40 {
            tags.push((i, format!("ab_{i}"), 10));
        }
        let mut idx = TagIndex::from_snapshot(tags, vec![]);
        idx.ensure_sorted();

        let (long_items, _) = idx.suggest_aliases(5, 0, Some("abcdefghij"));
        let long_cands: Vec<&str> = long_items[0]
            .candidates
            .iter()
            .map(|c| c.name.as_str())
            .collect();
        assert!(long_cands.contains(&"ab"));

        // Single CJK char "空" (3 UTF-8 bytes, 1 char) must NOT match via `cand_chars >= 2`
        let (cjk_items, _) = idx.suggest_aliases(5, 0, Some("星空物語"));
        assert!(cjk_items.is_empty());
    }
}
