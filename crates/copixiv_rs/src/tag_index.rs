use std::mem::size_of;

use memchr::memmem::Finder;
use rayon::prelude::*;
use roaring::RoaringBitmap;
use rustc_hash::{FxHashMap, FxHashSet};

use crate::interner::StringInterner;
use crate::model::{TagCandidate, TagSuggestItem};
use crate::postings::{PostingHandle, PostingSlab};
use crate::text_index::{collect_query_char_bigrams, contains_ascii_ci};

const MAX_CANDIDATES: usize = 50;
const PREFIX_CANDIDATE_CAP: usize = 200;
const SCAN_FLOOR: usize = 20;
const SCAN_CAP: usize = 200;

/// In-process tag similarity index (~76k tags), lazily derived from
/// `EngineState`'s `tag_interner`, `tag_postings`, and `aliased_tag_names`.
pub struct TagIndex {
    live_count: usize,
    aliased_tids: RoaringBitmap,
    sorted_tids: Vec<u32>,
    pub dirty: bool,
    first_char_index: FxHashMap<char, Vec<u32>>,
    char_bigrams: FxHashMap<u64, PostingHandle>,
    bigram_slab: PostingSlab,
}

impl Default for TagIndex {
    fn default() -> Self {
        Self {
            live_count: 0,
            aliased_tids: RoaringBitmap::new(),
            sorted_tids: Vec::new(),
            dirty: true,
            first_char_index: FxHashMap::default(),
            char_bigrams: FxHashMap::default(),
            bigram_slab: PostingSlab::new(),
        }
    }
}

#[inline]
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
    pub fn build_from_state(
        tag_interner: &StringInterner,
        tag_postings: &[PostingHandle],
        postings_slab: &PostingSlab,
        aliased_tag_names: &FxHashSet<String>,
    ) -> Self {
        let total = tag_postings.len().min(tag_interner.len());
        let mut live_tids: Vec<(u32, i32)> = Vec::with_capacity(total);
        let mut char_bigrams: FxHashMap<u64, PostingHandle> = FxHashMap::default();
        let mut bigram_slab = PostingSlab::new();
        let mut bg_scratch = Vec::with_capacity(32);

        for (tid_usize, &handle) in tag_postings.iter().enumerate().take(total) {
            let rc = handle.len(postings_slab) as i32;
            if rc <= 0 {
                continue;
            }
            let tid = tid_usize as u32;
            live_tids.push((tid, rc));

            let name = tag_interner.get_str(tid);
            char_bigrams_of_lower(name, &mut bg_scratch);
            for &bg in &bg_scratch {
                let h = char_bigrams.entry(bg).or_default();
                bigram_slab.insert(h, tid);
            }
        }

        // Order by `(reference_count DESC, id DESC)`; since `id = (tid as i64) + 1`,
        // descending `tid` is equivalent to descending `id`.
        live_tids.par_sort_unstable_by(|a, b| b.1.cmp(&a.1).then_with(|| b.0.cmp(&a.0)));
        let sorted_tids: Vec<u32> = live_tids.iter().map(|&(tid, _)| tid).collect();

        let mut first_char_index: FxHashMap<char, Vec<u32>> = FxHashMap::default();
        for &tid in &sorted_tids {
            if let Some(ch) = tag_interner.get_str(tid).chars().next() {
                first_char_index
                    .entry(ch.to_ascii_lowercase())
                    .or_default()
                    .push(tid);
            }
        }

        let mut aliased_tids = RoaringBitmap::new();
        for name in aliased_tag_names {
            if let Some(tid) = tag_interner.get_id(name) {
                aliased_tids.insert(tid);
            }
        }

        let mut idx = Self {
            live_count: sorted_tids.len(),
            aliased_tids,
            sorted_tids,
            dirty: false,
            first_char_index,
            char_bigrams,
            bigram_slab,
        };
        idx.compact_and_shrink();
        idx
    }

    fn compact_and_shrink(&mut self) {
        self.sorted_tids.shrink_to_fit();
        for v in self.first_char_index.values_mut() {
            v.shrink_to_fit();
        }
        self.first_char_index.shrink_to_fit();
        self.bigram_slab.compact(self.char_bigrams.values_mut());
        self.char_bigrams.shrink_to_fit();
    }

    pub fn refresh_aliased(
        &mut self,
        tag_interner: &StringInterner,
        aliased_tag_names: &FxHashSet<String>,
    ) {
        self.aliased_tids.clear();
        for name in aliased_tag_names {
            if let Some(tid) = tag_interner.get_id(name) {
                self.aliased_tids.insert(tid);
            }
        }
    }

    pub fn tag_count(&self) -> usize {
        self.live_count
    }

    pub fn approx_bytes(&self) -> usize {
        let vec_bytes = self.sorted_tids.capacity() * size_of::<u32>();
        let map_bytes = self.aliased_tids.serialized_size()
            + self.first_char_index.capacity() * (size_of::<char>() + size_of::<Vec<u32>>())
            + self
                .first_char_index
                .values()
                .map(|v| v.capacity() * size_of::<u32>())
                .sum::<usize>()
            + self.char_bigrams.capacity() * (size_of::<u64>() + size_of::<PostingHandle>() + 1)
            + self.bigram_slab.approx_heap_bytes();
        vec_bytes + map_bytes
    }

    #[inline]
    fn to_dto(
        tid: u32,
        tag_interner: &StringInterner,
        tag_postings: &[PostingHandle],
        postings_slab: &PostingSlab,
    ) -> TagCandidate {
        TagCandidate {
            id: (tid as i64) + 1,
            name: tag_interner.get_str(tid).to_owned(),
            reference_count: tag_postings[tid as usize].len(postings_slab) as i32,
        }
    }

    #[inline]
    fn ref_count_of(tid: u32, tag_postings: &[PostingHandle], postings_slab: &PostingSlab) -> i32 {
        tag_postings
            .get(tid as usize)
            .map_or(0, |h| h.len(postings_slab) as i32)
    }

    fn suggest_for_tid(
        &self,
        target_tid: u32,
        tag_interner: &StringInterner,
        tag_postings: &[PostingHandle],
        postings_slab: &PostingSlab,
    ) -> Vec<TagCandidate> {
        let tgt_name = tag_interner.get_str(target_tid);
        if tgt_name.is_empty() {
            return Vec::new();
        }
        let tgt_lower = tgt_name.to_ascii_lowercase();
        let tgt_chars = tgt_lower.chars().count();
        let mut tgt_bigrams = Vec::with_capacity(tgt_chars);
        char_bigrams_of_lower(&tgt_lower, &mut tgt_bigrams);

        // tid -> highest recall tier (3: superstring, 2: substring/jaccard>=0.5, 1: prefix)
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
                for cand_tid in cand_bm.iter() {
                    if cand_tid == target_tid {
                        continue;
                    }
                    if Self::ref_count_of(cand_tid, tag_postings, postings_slab) <= 0
                        || self.aliased_tids.contains(cand_tid)
                    {
                        continue;
                    }
                    if contains_ascii_ci(
                        tag_interner.get_str(cand_tid).as_bytes(),
                        tgt_lower.len(),
                        &finder,
                        need_lower,
                    ) {
                        matched.insert(cand_tid, 3);
                    }
                }
            }
        } else {
            // Single-char or whitespace-only target tag: scan sorted tids directly.
            for &cand_tid in &self.sorted_tids {
                if cand_tid == target_tid {
                    continue;
                }
                if Self::ref_count_of(cand_tid, tag_postings, postings_slab) <= 0
                    || self.aliased_tids.contains(cand_tid)
                {
                    continue;
                }
                if contains_ascii_ci(
                    tag_interner.get_str(cand_tid).as_bytes(),
                    tgt_lower.len(),
                    &finder,
                    need_lower,
                ) {
                    matched.insert(cand_tid, 3);
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
            let mut probe_tids: FxHashSet<u32> = FxHashSet::default();
            for &(_, handle) in &by_len[..probe_count] {
                for cand_tid in handle.iter(&self.bigram_slab) {
                    if cand_tid != target_tid && !matched.contains_key(&cand_tid) {
                        probe_tids.insert(cand_tid);
                    }
                }
            }
            let mut cand_bgs = Vec::with_capacity(32);
            for &cand_tid in &probe_tids {
                let cand_name = tag_interner.get_str(cand_tid);
                if Self::ref_count_of(cand_tid, tag_postings, postings_slab) <= 0
                    || cand_name.is_empty()
                    || self.aliased_tids.contains(cand_tid)
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
                    sim_cache.insert(cand_tid, sim);
                    matched
                        .entry(cand_tid)
                        .and_modify(|t| *t = (*t).max(2))
                        .or_insert(2);
                }
            }
            // High-frequency bigrams skipped by `probe_count` can still match short
            // reverse-containment substrings (`tgt.contains(cand)`).
            for &(_, handle) in &by_len[probe_count..] {
                for cand_tid in handle.iter(&self.bigram_slab) {
                    if cand_tid == target_tid
                        || matched.contains_key(&cand_tid)
                        || probe_tids.contains(&cand_tid)
                    {
                        continue;
                    }
                    let cand_name = tag_interner.get_str(cand_tid);
                    if Self::ref_count_of(cand_tid, tag_postings, postings_slab) <= 0
                        || cand_name.len() >= tgt_lower.len()
                        || self.aliased_tids.contains(cand_tid)
                    {
                        continue;
                    }
                    if cand_name.chars().count() >= 2
                        && contains_raw_needle_ci(tgt_lower.as_bytes(), cand_name.as_bytes())
                    {
                        matched.insert(cand_tid, 2);
                    }
                }
            }
        }

        // 3. First-char prefix matches (top eligible rows ordered by rc DESC, id DESC).
        if let Some(fc) = tgt_lower.chars().next() {
            if let Some(bucket) = self.first_char_index.get(&fc) {
                let mut taken = 0usize;
                for &cand_tid in bucket {
                    if cand_tid == target_tid {
                        continue;
                    }
                    if Self::ref_count_of(cand_tid, tag_postings, postings_slab) <= 0
                        || self.aliased_tids.contains(cand_tid)
                    {
                        continue;
                    }
                    matched.entry(cand_tid).or_insert(1);
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
        let mut scored: Vec<(u8, u32, i32, u32)> = Vec::with_capacity(matched.len());
        for (cand_tid, tier) in matched {
            let sim = if let Some(&cached) = sim_cache.get(&cand_tid) {
                cached
            } else {
                let cand_name = tag_interner.get_str(cand_tid);
                char_bigrams_of_lower(cand_name, &mut cand_bgs);
                let (s, _) = jaccard_permille(
                    &tgt_bigrams,
                    &cand_bgs,
                    tgt_chars,
                    cand_name.chars().count(),
                );
                s
            };
            let rc = Self::ref_count_of(cand_tid, tag_postings, postings_slab);
            scored.push((tier, sim, rc, cand_tid));
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
            .map(|(_, _, _, tid)| Self::to_dto(tid, tag_interner, tag_postings, postings_slab))
            .collect()
    }

    pub fn suggest_aliases(
        &self,
        tag_interner: &StringInterner,
        tag_postings: &[PostingHandle],
        postings_slab: &PostingSlab,
        limit: usize,
        offset: i64,
        target_tag: Option<&str>,
    ) -> (Vec<TagSuggestItem>, Option<i64>) {
        debug_assert!(!self.dirty);

        if let Some(name) = target_tag.filter(|s| !s.is_empty()) {
            let Some(tid) = tag_interner.get_id(name) else {
                return (Vec::new(), None);
            };
            if Self::ref_count_of(tid, tag_postings, postings_slab) <= 0
                || self.aliased_tids.contains(tid)
            {
                return (Vec::new(), None);
            }
            let candidates = self.suggest_for_tid(tid, tag_interner, tag_postings, postings_slab);
            if candidates.is_empty() {
                return (Vec::new(), None);
            }
            return (
                vec![TagSuggestItem {
                    target: Self::to_dto(tid, tag_interner, tag_postings, postings_slab),
                    candidates,
                }],
                None,
            );
        }

        if limit == 0 {
            return (Vec::new(), None);
        }

        let total = tag_postings.len().min(tag_interner.len());
        let scan_limit = (limit * 4).clamp(SCAN_FLOOR, SCAN_CAP);
        let start_idx = if offset <= 0 {
            0usize
        } else {
            let Ok(cur_tid_usize) = usize::try_from(offset - 1) else {
                return (Vec::new(), None);
            };
            if cur_tid_usize >= total {
                return (Vec::new(), None);
            }
            let cur_tid = cur_tid_usize as u32;
            let cur_rc = Self::ref_count_of(cur_tid, tag_postings, postings_slab);
            if cur_rc <= 0 {
                // Option 2 zeroed the cursor tag's ref_count (or tag was removed):
                // partition_point with cur_rc=0 would jump to the 0-rc tail and
                // truncate prematurely. Restart from 0 (already-aliased tags are skipped).
                0usize
            } else {
                self.sorted_tids.partition_point(|&tid| {
                    let rc = Self::ref_count_of(tid, tag_postings, postings_slab);
                    (rc, tid) >= (cur_rc, cur_tid)
                })
            }
        };

        let mut items: Vec<TagSuggestItem> = Vec::with_capacity(limit);
        let mut scanned = 0usize;
        let mut last_scanned_id: Option<i64> = None;

        for &tid in &self.sorted_tids[start_idx..] {
            let rc = Self::ref_count_of(tid, tag_postings, postings_slab);
            if rc <= 0 {
                break; // `sorted_tids` is descending by reference_count
            }
            if self.aliased_tids.contains(tid) {
                continue;
            }
            let ext_id = (tid as i64) + 1;
            scanned += 1;
            last_scanned_id = Some(ext_id);
            let candidates = self.suggest_for_tid(tid, tag_interner, tag_postings, postings_slab);
            if !candidates.is_empty() {
                items.push(TagSuggestItem {
                    target: Self::to_dto(tid, tag_interner, tag_postings, postings_slab),
                    candidates,
                });
                if items.len() >= limit {
                    return (items, Some(ext_id));
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

    struct TestTagState {
        interner: StringInterner,
        postings: Vec<PostingHandle>,
        slab: PostingSlab,
        aliased: FxHashSet<String>,
    }

    impl TestTagState {
        fn from_counts(tags: &[(&str, i32)], aliased: &[&str]) -> Self {
            let mut interner = StringInterner::new();
            let mut postings = Vec::new();
            let mut slab = PostingSlab::new();
            for &(name, rc) in tags {
                let tid = interner.intern(name) as usize;
                if tid >= postings.len() {
                    postings.resize(tid + 1, PostingHandle::EMPTY);
                }
                for doc in 0..rc {
                    slab.insert(&mut postings[tid], doc as u32);
                }
            }
            Self {
                interner,
                postings,
                slab,
                aliased: aliased.iter().map(|s| (*s).to_string()).collect(),
            }
        }

        fn set_count(&mut self, name: &str, rc: i32) {
            let tid = self.interner.intern(name) as usize;
            if tid >= self.postings.len() {
                self.postings.resize(tid + 1, PostingHandle::EMPTY);
            }
            self.postings[tid] = PostingHandle::EMPTY;
            for doc in 0..rc {
                self.slab.insert(&mut self.postings[tid], doc as u32);
            }
        }

        fn build_index(&self) -> TagIndex {
            TagIndex::build_from_state(&self.interner, &self.postings, &self.slab, &self.aliased)
        }
    }

    #[test]
    fn tag_index_suggests_substring_similarity_prefix_and_respects_aliases_and_special_chars() {
        let mut st = TestTagState::from_counts(
            &[
                ("百合", 100),
                ("百合漫画", 50),
                ("真百合小説", 10),
                ("日常", 80),
                ("%高中生", 40),
                ("%高中生女子", 5),
                ("aliased_src", 90),
                ("BanG_Dream!", 60),
                ("BanG_Dream_Live", 15),
            ],
            &["aliased_src"],
        );
        let idx = st.build_index();

        let (items, next_off) =
            idx.suggest_aliases(&st.interner, &st.postings, &st.slab, 5, 0, Some("%高中生"));
        assert_eq!(next_off, None);
        assert_eq!(items.len(), 1);
        let cand_names: Vec<&str> = items[0]
            .candidates
            .iter()
            .map(|c| c.name.as_str())
            .collect();
        assert!(cand_names.contains(&"%高中生女子"));
        assert!(!cand_names.contains(&"日常"));

        assert!(idx
            .suggest_aliases(
                &st.interner,
                &st.postings,
                &st.slab,
                5,
                0,
                Some("aliased_src")
            )
            .0
            .is_empty());

        let (yuri_items, _) =
            idx.suggest_aliases(&st.interner, &st.postings, &st.slab, 5, 0, Some("百合"));
        let yuri_cands: Vec<&str> = yuri_items[0]
            .candidates
            .iter()
            .map(|c| c.name.as_str())
            .collect();
        assert_eq!(yuri_cands[0], "百合漫画");
        assert!(yuri_cands.contains(&"真百合小説"));

        let (p1, cur1) = idx.suggest_aliases(&st.interner, &st.postings, &st.slab, 2, 0, None);
        assert_eq!(p1.len(), 2);
        let cur1 = cur1.expect("first page should return a cursor");
        let (p2, cur2) = idx.suggest_aliases(&st.interner, &st.postings, &st.slab, 10, cur1, None);
        assert!(!p2.is_empty());
        assert_eq!(cur2, None, "exhausted last batch must return None");
        let ids1: Vec<i64> = p1.iter().map(|i| i.target.id).collect();
        let ids2: Vec<i64> = p2.iter().map(|i| i.target.id).collect();
        for id in &ids2 {
            assert!(!ids1.contains(id));
        }

        assert_eq!(
            idx.suggest_aliases(&st.interner, &st.postings, &st.slab, 5, 999999, None),
            (Vec::new(), None)
        );

        // Simulate Option 2 zeroing the cursor tag's ref_count + marking it aliased:
        // scanning from cur1 must not jump to the 0-rc tail and truncate prematurely.
        let cursor_name = p1.last().unwrap().target.name.clone();
        st.set_count(&cursor_name, 0);
        st.aliased.insert(cursor_name);
        let idx_zeroed = st.build_index();
        let (after_zero, _) =
            idx_zeroed.suggest_aliases(&st.interner, &st.postings, &st.slab, 5, cur1, None);
        assert!(!after_zero.is_empty());
        assert!(after_zero.iter().all(|i| i.target.id != cur1));

        // Removing "日常" and adding "百合小説" updates derived TagIndex cleanly
        st.set_count("日常", 0);
        st.set_count("百合小説", 200);
        let idx_updated = st.build_index();
        let (after, _) =
            idx_updated.suggest_aliases(&st.interner, &st.postings, &st.slab, 5, 0, Some("百合"));
        assert!(after[0].candidates.iter().any(|c| c.name == "百合小説"));
    }

    #[test]
    fn tag_index_finds_high_freq_short_substring_on_long_target_and_ignores_single_cjk_char() {
        let mut pairs: Vec<(String, i32)> = vec![
            ("abcdefghij".into(), 500),
            ("ab".into(), 200),
            ("空".into(), 300),
            ("星空物語".into(), 400),
        ];
        for i in 10..40 {
            pairs.push((format!("ab_{i}"), 10));
        }
        let borrowed: Vec<(&str, i32)> = pairs.iter().map(|(s, c)| (s.as_str(), *c)).collect();
        let st = TestTagState::from_counts(&borrowed, &[]);
        let idx = st.build_index();

        let (long_items, _) = idx.suggest_aliases(
            &st.interner,
            &st.postings,
            &st.slab,
            5,
            0,
            Some("abcdefghij"),
        );
        let long_cands: Vec<&str> = long_items[0]
            .candidates
            .iter()
            .map(|c| c.name.as_str())
            .collect();
        assert!(long_cands.contains(&"ab"));

        // Single CJK char "空" (3 UTF-8 bytes, 1 char) must NOT match via `cand_chars >= 2`
        let (cjk_items, _) =
            idx.suggest_aliases(&st.interner, &st.postings, &st.slab, 5, 0, Some("星空物語"));
        assert!(cjk_items.is_empty());
    }
}
