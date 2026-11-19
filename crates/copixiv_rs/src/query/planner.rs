use std::cmp::Reverse;
use std::hash::{Hash, Hasher};

use roaring::RoaringBitmap;
use rustc_hash::FxHasher;

use crate::filter::{IntFilterField, QueryFilter, StrFilterField};
use crate::interner::StringInterner;
use crate::postings::PostingHandle;
use crate::query::mask::CandidateMask;
use crate::storage::EngineState;
use crate::text_index::{collect_query_char_bigrams, contains_ascii_ci, SegmentMatcher};

#[inline]
fn hash_bytes(bytes: &[u8]) -> u64 {
    let mut h = FxHasher::default();
    bytes.hash(&mut h);
    h.finish()
}

const PRECOMPUTE_INTERNER_THRESHOLD: u64 = 512;

pub enum ResidualMatcher {
    Plain(SegmentMatcher),
    WithFlags {
        matcher: SegmentMatcher,
        tags: Vec<bool>,
        author_names: Vec<bool>,
        series_names: Vec<bool>,
    },
}

impl EngineState {
    fn build_residual_matcher(&self, matcher: SegmentMatcher, cand_len: u64) -> ResidualMatcher {
        if cand_len > PRECOMPUTE_INTERNER_THRESHOLD && !matcher.has_whitespace {
            let nl = matcher.lower.len();
            let need_lower = matcher.need_lower;
            let finder = &matcher.finder;
            let tags = self
                .tag_interner
                .matching_ids(|s| contains_ascii_ci(s.as_bytes(), nl, finder, need_lower));
            let author_names = self
                .author_name_interner
                .matching_ids(|s| contains_ascii_ci(s.as_bytes(), nl, finder, need_lower));
            let series_names = self
                .series_name_interner
                .matching_ids(|s| contains_ascii_ci(s.as_bytes(), nl, finder, need_lower));
            ResidualMatcher::WithFlags {
                matcher,
                tags,
                author_names,
                series_names,
            }
        } else {
            ResidualMatcher::Plain(matcher)
        }
    }

    fn narrow_by_contiguous_token(
        &self,
        matcher: &SegmentMatcher,
        bigrams: &[u64],
        mask: &mut CandidateMask,
    ) {
        let mut token_hits = RoaringBitmap::new();
        for (id, &handle) in self.tag_postings.iter().enumerate() {
            if !handle.is_empty()
                && matcher.matches_str(self.tag_interner.get_str(id as u32).as_bytes())
            {
                handle.union_into(&self.postings_slab, &mut token_hits);
            }
        }
        for (id, &handle) in self.author_name_postings.iter().enumerate() {
            if !handle.is_empty()
                && matcher.matches_str(self.author_name_interner.get_str(id as u32).as_bytes())
            {
                handle.union_into(&self.postings_slab, &mut token_hits);
            }
        }
        for (id, &handle) in self.series_name_postings.iter().enumerate() {
            if !handle.is_empty()
                && matcher.matches_str(self.series_name_interner.get_str(id as u32).as_bytes())
            {
                handle.union_into(&self.postings_slab, &mut token_hits);
            }
        }

        let mut all_bgs_present = true;
        let mut ranked_bgs: Vec<(usize, PostingHandle)> = Vec::with_capacity(bigrams.len());
        for &bg in bigrams {
            let Some(handle) = self.text_index.get(bg) else {
                all_bgs_present = false;
                break;
            };
            let len = handle.len(&self.text_index.slab);
            if len == 0 {
                all_bgs_present = false;
                break;
            }
            ranked_bgs.push((len, handle));
        }

        if all_bgs_present && !ranked_bgs.is_empty() {
            ranked_bgs.sort_unstable_by_key(|(len, _)| *len);
            let title_bm = match mask {
                CandidateMask::Bitmap(cur) if (cur.len() as usize) < ranked_bgs[0].0 => {
                    let mut bm = cur.clone();
                    for &(_, handle) in &ranked_bgs {
                        if !handle.intersect_bitmap(&self.text_index.slab, &mut bm) {
                            break;
                        }
                    }
                    bm
                }
                _ => {
                    let mut bm = ranked_bgs[0].1.to_bitmap(&self.text_index.slab);
                    for (i, &(_, handle)) in ranked_bgs.iter().enumerate().skip(1) {
                        if i >= 11 || bm.len() <= 32 {
                            break;
                        }
                        if !handle.intersect_bitmap(&self.text_index.slab, &mut bm) {
                            break;
                        }
                    }
                    if let CandidateMask::Bitmap(cur) = mask {
                        bm &= &*cur;
                    }
                    bm
                }
            };
            for doc in title_bm.iter() {
                if !token_hits.contains(doc) && matcher.matches_str(self.title_arena.get_bytes(doc))
                {
                    token_hits.insert(doc);
                }
            }
        }

        debug_assert!(token_hits.is_subset(&self.alive));
        mask.intersect_bm(&token_hits);
    }

    /// Resolve structural + field-interner + title-bigram candidate bitmap and residual matchers.
    pub fn resolve_candidates(
        &self,
        filter: &QueryFilter,
    ) -> (CandidateMask, Vec<ResidualMatcher>) {
        let mut lower_segments: Vec<String> = Vec::with_capacity(filter.keyword_segments.len());
        for seg in &filter.keyword_segments {
            let low = seg.to_ascii_lowercase();
            if !low.is_empty() && !lower_segments.contains(&low) {
                lower_segments.push(low);
            }
        }

        let exclude_active = filter
            .exclude_blocked_tags
            .unwrap_or(self.exclude_blocked_default);

        if filter.count_blocked && (!exclude_active || self.blocked_bitmap.is_empty()) {
            return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
        }

        let mut mask = CandidateMask::AllAlive;

        if let Some(ref rids) = filter.restrict_ids {
            let mut bm = RoaringBitmap::new();
            for nid in rids {
                if let Some(&doc) = self.id_to_doc.get(nid) {
                    if self.alive.contains(doc) {
                        bm.insert(doc);
                    }
                }
            }
            if bm.is_empty() {
                return (CandidateMask::Bitmap(bm), Vec::new());
            }
            mask = CandidateMask::Bitmap(bm);
        }

        for &(field, val) in &filter.int_filters {
            match field {
                IntFilterField::Id => {
                    let Some(&doc) = self.id_to_doc.get(&val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    };
                    if !self.alive.contains(doc) || !mask.contains(doc) {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    }
                    let mut bm = RoaringBitmap::new();
                    bm.insert(doc);
                    mask = CandidateMask::Bitmap(bm);
                }
                IntFilterField::AuthorId => {
                    let Some(&h) = self.author_postings.get(&val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    };
                    mask.intersect_posting(h, &self.postings_slab);
                }
                IntFilterField::SeriesId => {
                    let Some(&h) = self.series_postings.get(&val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    };
                    mask.intersect_posting(h, &self.postings_slab);
                }
                IntFilterField::HasEpub => {
                    let Ok(val_u8) = u8::try_from(val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    };
                    let Some(&h) = self.epub_postings.get(&val_u8) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    };
                    mask.intersect_posting(h, &self.postings_slab);
                }
                IntFilterField::Like
                | IntFilterField::View
                | IntFilterField::Text
                | IntFilterField::SeriesIndex => {}
            }
            if mask.is_empty(&self.alive) {
                return (mask, Vec::new());
            }
        }

        if filter.only_favourite {
            mask.intersect_bm(&self.fav_bitmap);
            if mask.is_empty(&self.alive) {
                return (mask, Vec::new());
            }
        }
        if filter.only_special_follow {
            mask.intersect_bm(&self.sf_bitmap);
            if mask.is_empty(&self.alive) {
                return (mask, Vec::new());
            }
        }

        if filter.count_blocked {
            mask.intersect_bm(&self.blocked_bitmap);
            if mask.is_empty(&self.alive) {
                return (mask, Vec::new());
            }
        }

        for tag in &filter.tags {
            let Some(tid) = self.tag_interner.get_id(tag) else {
                return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
            };
            mask.intersect_posting(self.tag_postings[tid as usize], &self.postings_slab);
            if mask.is_empty(&self.alive) {
                return (mask, Vec::new());
            }
        }

        for (field, val) in &filter.str_filters {
            match field {
                StrFilterField::Source => {
                    let Some(id) = self.source_interner.get_id(val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    };
                    mask.intersect_posting(self.source_postings[id as usize], &self.postings_slab);
                }
                StrFilterField::AuthorName => {
                    let Some(id) = self.author_name_interner.get_id(val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    };
                    mask.intersect_posting(
                        self.author_name_postings[id as usize],
                        &self.postings_slab,
                    );
                }
                StrFilterField::SeriesName => {
                    let Some(id) = self.series_name_interner.get_id(val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    };
                    mask.intersect_posting(
                        self.series_name_postings[id as usize],
                        &self.postings_slab,
                    );
                }
                StrFilterField::Title => {
                    let th = hash_bytes(val.as_bytes());
                    let Some(bucket) = self.title_buckets.get(th) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), Vec::new());
                    };
                    let mut exact = RoaringBitmap::new();
                    for &doc in bucket.as_slice() {
                        if mask.contains_slot(doc, &self.alive)
                            && self.title_arena.get_str(doc) == val
                        {
                            exact.insert(doc);
                        }
                    }
                    mask = CandidateMask::Bitmap(exact);
                }
            }
            if mask.is_empty(&self.alive) {
                return (mask, Vec::new());
            }
        }

        for &(field, val) in &filter.int_filters {
            match field {
                IntFilterField::Like => {
                    let filtered: RoaringBitmap = mask
                        .as_bitmap(&self.alive)
                        .iter()
                        .filter(|&d| (self.likes[d as usize] as i64) == val)
                        .collect();
                    mask = CandidateMask::Bitmap(filtered);
                }
                IntFilterField::View => {
                    let filtered: RoaringBitmap = mask
                        .as_bitmap(&self.alive)
                        .iter()
                        .filter(|&d| (self.views[d as usize] as i64) == val)
                        .collect();
                    mask = CandidateMask::Bitmap(filtered);
                }
                IntFilterField::Text => {
                    let filtered: RoaringBitmap = mask
                        .as_bitmap(&self.alive)
                        .iter()
                        .filter(|&d| (self.texts[d as usize] as i64) == val)
                        .collect();
                    mask = CandidateMask::Bitmap(filtered);
                }
                IntFilterField::SeriesIndex => {
                    let filtered: RoaringBitmap = mask
                        .as_bitmap(&self.alive)
                        .iter()
                        .filter(|&d| {
                            !self.null_series_index.contains(d)
                                && (self.series_index[d as usize] as i64) == val
                        })
                        .collect();
                    mask = CandidateMask::Bitmap(filtered);
                }
                _ => {}
            }
            if mask.is_empty(&self.alive) {
                return (mask, Vec::new());
            }
        }

        if !filter.count_blocked && exclude_active {
            mask.subtract_bm(&self.blocked_bitmap, &self.alive);
            if mask.is_empty(&self.alive) {
                return (mask, Vec::new());
            }
        }

        if let Some(ref ex) = filter.exclude_ids {
            if !ex.is_empty() {
                let mut ex_bm = RoaringBitmap::new();
                for nid in ex {
                    if let Some(&doc) = self.id_to_doc.get(nid) {
                        ex_bm.insert(doc);
                    }
                }
                mask.subtract_bm(&ex_bm, &self.alive);
                if mask.is_empty(&self.alive) {
                    return (mask, Vec::new());
                }
            }
        }

        if lower_segments.is_empty() {
            return (mask, Vec::new());
        }

        // Narrow with selective non-whitespace multi-char segments before single-char/whitespace checks.
        lower_segments.sort_by_key(|s| (s.chars().any(char::is_whitespace), Reverse(s.len())));

        let mut matchers = Vec::with_capacity(lower_segments.len());
        let mut bg_scratch = Vec::with_capacity(32);

        for seg in lower_segments {
            let matcher = SegmentMatcher::new(seg);
            if !matcher.has_whitespace {
                collect_query_char_bigrams(&matcher.lower, &mut bg_scratch);
                if !bg_scratch.is_empty() && mask.len(&self.alive) > PRECOMPUTE_INTERNER_THRESHOLD {
                    self.narrow_by_contiguous_token(&matcher, &bg_scratch, &mut mask);
                    if mask.is_empty(&self.alive) {
                        return (mask, Vec::new());
                    }
                    // Already verified exhaustively across interners + title_arena.
                    continue;
                }
            } else if mask.len(&self.alive) > PRECOMPUTE_INTERNER_THRESHOLD {
                for tok in matcher.lower.split_whitespace() {
                    collect_query_char_bigrams(tok, &mut bg_scratch);
                    if !bg_scratch.is_empty() {
                        let sub_m = SegmentMatcher::new(tok.to_owned());
                        self.narrow_by_contiguous_token(&sub_m, &bg_scratch, &mut mask);
                        if mask.is_empty(&self.alive) {
                            return (mask, Vec::new());
                        }
                        if mask.len(&self.alive) <= PRECOMPUTE_INTERNER_THRESHOLD {
                            break;
                        }
                    }
                }
            }

            let cand_len = mask.len(&self.alive);
            matchers.push(self.build_residual_matcher(matcher, cand_len));
        }

        (mask, matchers)
    }

    #[inline]
    pub fn doc_matches_segment(&self, doc: u32, rm: &ResidualMatcher) -> bool {
        let (m, flags) = match rm {
            ResidualMatcher::Plain(m) => (m, None),
            ResidualMatcher::WithFlags {
                matcher,
                tags,
                author_names,
                series_names,
            } => (matcher, Some((tags, author_names, series_names))),
        };
        let idx = doc as usize;
        if m.has_whitespace {
            let an_id = self.doc_author_name[idx];
            let an =
                (an_id != StringInterner::NONE).then(|| self.author_name_interner.get_str(an_id));
            let sn_id = self.doc_series_name[idx];
            let sn =
                (sn_id != StringInterner::NONE).then(|| self.series_name_interner.get_str(sn_id));
            let joined = crate::text_index::build_search_lower(
                self.title_arena.get_str(doc),
                an,
                sn,
                self.doc_tags
                    .get(doc)
                    .iter()
                    .map(|&tid| self.tag_interner.get_str(tid)),
            );
            return m.finder.find(joined.as_bytes()).is_some();
        }

        if m.matches_str(self.title_arena.get_bytes(doc)) {
            return true;
        }

        let an_id = self.doc_author_name[idx];
        if an_id != StringInterner::NONE {
            let matched = match flags {
                Some((_, an_flags, _)) => an_flags[an_id as usize],
                None => m.matches_str(self.author_name_interner.get_str(an_id).as_bytes()),
            };
            if matched {
                return true;
            }
        }

        let sn_id = self.doc_series_name[idx];
        if sn_id != StringInterner::NONE {
            let matched = match flags {
                Some((_, _, sn_flags)) => sn_flags[sn_id as usize],
                None => m.matches_str(self.series_name_interner.get_str(sn_id).as_bytes()),
            };
            if matched {
                return true;
            }
        }

        let tags = self.doc_tags.get(doc);
        if let Some((tag_flags, _, _)) = flags {
            if tags.iter().any(|&tid| tag_flags[tid as usize]) {
                return true;
            }
        } else if tags
            .iter()
            .any(|&tid| m.matches_str(self.tag_interner.get_str(tid).as_bytes()))
        {
            return true;
        }

        false
    }

    #[inline]
    pub fn doc_passes_inline(
        &self,
        doc: u32,
        min_like: i64,
        min_text: i64,
        matchers: &[ResidualMatcher],
    ) -> bool {
        if min_like > 0 && (self.likes[doc as usize] as i64) < min_like {
            return false;
        }
        if min_text > 0 && (self.texts[doc as usize] as i64) < min_text {
            return false;
        }
        if matchers.is_empty() {
            return true;
        }
        matchers.iter().all(|m| self.doc_matches_segment(doc, m))
    }
}
