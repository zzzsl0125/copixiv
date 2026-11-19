use rayon::prelude::*;
use roaring::RoaringBitmap;
use rustc_hash::FxHashSet;

use crate::filter::{
    HotTrack, NumericRankItem, QueryFilter, SortAxis, StringRankItem, TrackCursor,
};
use crate::model::QueryPage;
use crate::query::mask::CandidateMask;
use crate::query::planner::ResidualMatcher;
use crate::random::{
    collect_top_k, fresh_seed, hex16_encode, top_k_random_bitmap, top_k_random_parallel,
    top_k_random_slice,
};
use crate::storage::EngineState;

impl EngineState {
    #[inline]
    fn min_bound_track_end(&self, hot: HotTrack, min_like: i64, min_text: i64) -> usize {
        let track = &self.tracks[hot as usize];
        match hot {
            HotTrack::Like if min_like > 0 => {
                track.partition_point(|&doc| (self.likes[doc as usize] as i64) >= min_like)
            }
            HotTrack::Text if min_text > 0 => {
                track.partition_point(|&doc| (self.texts[doc as usize] as i64) >= min_text)
            }
            _ => track.len(),
        }
    }

    fn verify_all_matching(
        &self,
        mask: &CandidateMask,
        min_like: i64,
        min_text: i64,
        matchers: &[ResidualMatcher],
    ) -> RoaringBitmap {
        let base_bm = mask.as_bitmap(&self.alive);
        if matchers.is_empty() && min_like <= 0 && min_text <= 0 {
            return base_bm.clone();
        }

        let docs: Vec<u32> = base_bm.iter().collect();
        if docs.len() >= 2048 && !matchers.is_empty() {
            let chunk_matched: Vec<Vec<u32>> = docs
                .par_chunks(1024)
                .map(|chunk| {
                    chunk
                        .iter()
                        .copied()
                        .filter(|&d| self.doc_passes_inline(d, min_like, min_text, matchers))
                        .collect()
                })
                .collect();
            let mut bm = RoaringBitmap::new();
            for part in chunk_matched {
                for d in part {
                    bm.push(d);
                }
            }
            bm
        } else {
            let mut bm = RoaringBitmap::new();
            for d in docs {
                if self.doc_passes_inline(d, min_like, min_text, matchers) {
                    bm.push(d);
                }
            }
            bm
        }
    }

    pub fn count_novels(&self, filter: &QueryFilter, cap: usize) -> (usize, bool) {
        let (mask, matchers) = self.resolve_candidates(filter);
        let base_bm = mask.as_bitmap(&self.alive);
        if base_bm.is_empty() {
            return (0, false);
        }

        let min_like = filter.min_like.unwrap_or(0);
        let min_text = filter.min_text.unwrap_or(0);

        if matchers.is_empty() && min_like <= 0 && min_text <= 0 {
            let total = base_bm.len();
            return ((total as usize).min(cap), total > cap as u64);
        }

        // Exact O(log N) binary-search counts when unfiltered by keywords/bitmaps.
        if matchers.is_empty() && matches!(mask, CandidateMask::AllAlive) {
            if min_like > 0 && min_text <= 0 && !self.tracks_dirty[HotTrack::Like as usize] {
                let cnt = self.min_bound_track_end(HotTrack::Like, min_like, 0);
                return (cnt.min(cap), cnt > cap);
            }
            if min_text > 0 && min_like <= 0 && !self.tracks_dirty[HotTrack::Text as usize] {
                let cnt = self.min_bound_track_end(HotTrack::Text, 0, min_text);
                return (cnt.min(cap), cnt > cap);
            }
        }

        // Choose the narrowest pre-sorted slice when clean `Like`/`Text` bounds beat scanning `base_bm`.
        let like_slice = (min_like > 0 && !self.tracks_dirty[HotTrack::Like as usize]).then(|| {
            let end = self.min_bound_track_end(HotTrack::Like, min_like, 0);
            &self.tracks[HotTrack::Like as usize][..end]
        });
        let text_slice = (min_text > 0 && !self.tracks_dirty[HotTrack::Text as usize]).then(|| {
            let end = self.min_bound_track_end(HotTrack::Text, 0, min_text);
            &self.tracks[HotTrack::Text as usize][..end]
        });

        let best_track_slice = match (like_slice, text_slice) {
            (Some(a), Some(b)) => Some(if a.len() <= b.len() { a } else { b }),
            (Some(a), None) => Some(a),
            (None, Some(b)) => Some(b),
            (None, None) => None,
        };

        if let Some(slice) = best_track_slice {
            if slice.len() <= base_bm.len() as usize {
                let mut matched = 0usize;
                for &doc in slice {
                    if (matches!(mask, CandidateMask::AllAlive) || base_bm.contains(doc))
                        && self.doc_passes_inline(doc, min_like, min_text, &matchers)
                    {
                        matched += 1;
                        if matched > cap {
                            return (cap, true);
                        }
                    }
                }
                return (matched, false);
            }
        }

        let mut matched = 0usize;
        for doc in base_bm.iter() {
            if self.doc_passes_inline(doc, min_like, min_text, &matchers) {
                matched += 1;
                if matched > cap {
                    return (cap, true);
                }
            }
        }
        (matched, false)
    }

    pub fn has_match(&self, filter: &QueryFilter) -> bool {
        self.count_novels(filter, 0).1
    }

    pub fn list_matching_ids(&self, filter: &QueryFilter) -> Vec<i64> {
        let (mask, matchers) = self.resolve_candidates(filter);
        let min_like = filter.min_like.unwrap_or(0);
        let min_text = filter.min_text.unwrap_or(0);
        let bm = self.verify_all_matching(&mask, min_like, min_text, &matchers);
        let mut ids: Vec<i64> = bm.iter().map(|d| self.doc_ids[d as usize]).collect();
        ids.par_sort_unstable();
        ids
    }

    pub fn list_blocked_ids(&self) -> Vec<i64> {
        if !self.exclude_blocked_default || self.blocked_bitmap.is_empty() {
            return Vec::new();
        }
        let mut ids: Vec<i64> = self
            .blocked_bitmap
            .iter()
            .map(|d| self.doc_ids[d as usize])
            .collect();
        ids.sort_unstable();
        ids
    }

    pub fn sort_ids(&self, novel_ids: &[i64], axis: SortAxis, descending: bool) -> Vec<i64> {
        if novel_ids.is_empty() {
            return Vec::new();
        }
        let mut docs: Vec<u32> = Vec::with_capacity(novel_ids.len());
        let mut seen = FxHashSet::with_capacity_and_hasher(novel_ids.len(), Default::default());
        for &nid in novel_ids {
            if let Some(&doc) = self.id_to_doc.get(&nid) {
                if self.alive.contains(doc) && seen.insert(doc) {
                    docs.push(doc);
                }
            }
        }
        match axis {
            SortAxis::Numeric(field) => {
                docs.sort_unstable_by(|&a, &b| {
                    let ka = self.numeric_key(field, a);
                    let kb = self.numeric_key(field, b);
                    if descending {
                        ka.cmp(&kb)
                    } else {
                        kb.cmp(&ka)
                    }
                });
            }
            SortAxis::String(field) => {
                docs.sort_unstable_by(|&a, &b| {
                    let (na, va) = self.string_val(field, a);
                    let (nb, vb) = self.string_val(field, b);
                    let ia = StringRankItem {
                        descending,
                        is_null: na,
                        val: va,
                        novel_id: self.doc_ids[a as usize],
                        doc: a,
                    };
                    let ib = StringRankItem {
                        descending,
                        is_null: nb,
                        val: vb,
                        novel_id: self.doc_ids[b as usize],
                        doc: b,
                    };
                    ia.cmp(&ib)
                });
            }
            SortAxis::None | SortAxis::Random => {}
        }
        docs.into_iter().map(|d| self.doc_ids[d as usize]).collect()
    }

    #[allow(clippy::too_many_arguments)]
    pub fn query_novels_slots_with_mask(
        &self,
        filter: &QueryFilter,
        mask: CandidateMask,
        matchers: Vec<ResidualMatcher>,
        axis: SortAxis,
        descending: bool,
        limit: usize,
        cursor: Option<&TrackCursor>,
    ) -> Result<QueryPage<u32>, String> {
        let base_bm = mask.as_bitmap(&self.alive);
        if base_bm.is_empty() {
            return Ok((Vec::new(), false, None));
        }

        let min_like = filter.min_like.unwrap_or(0);
        let min_text = filter.min_text.unwrap_or(0);

        match axis {
            SortAxis::Random => {
                let (seed, rand_cursor) = match cursor {
                    Some(TrackCursor::Random(s, h, cid)) => (*s, Some((*h, *cid))),
                    None => (fresh_seed(), None),
                    Some(_) => return Err("Expected Random cursor".into()),
                };

                let total_slots = self.doc_ids.len() as u64;
                let cand_len = base_bm.len();
                let (hits, has_more) = if limit == 0 || cand_len < 8192 {
                    top_k_random_bitmap(base_bm, &self.doc_ids, seed, rand_cursor, limit, |d| {
                        self.doc_passes_inline(d, min_like, min_text, &matchers)
                    })
                } else if cand_len.saturating_mul(2) >= total_slots {
                    top_k_random_parallel(
                        self.doc_ids.len() as u32,
                        &self.doc_ids,
                        seed,
                        rand_cursor,
                        limit,
                        |d| {
                            mask.contains_slot(d, &self.alive)
                                && self.doc_passes_inline(d, min_like, min_text, &matchers)
                        },
                    )
                } else {
                    let docs: Vec<u32> = base_bm.iter().collect();
                    top_k_random_slice(&docs, &self.doc_ids, seed, rand_cursor, limit, |d| {
                        self.doc_passes_inline(d, min_like, min_text, &matchers)
                    })
                };

                let rand_out = if has_more && !hits.is_empty() {
                    let (last_h, last_id, _) = hits[hits.len() - 1];
                    Some((hex16_encode(seed), hex16_encode(last_h), last_id))
                } else {
                    None
                };
                let slots = hits.into_iter().map(|(_, _, doc)| doc).collect();
                Ok((slots, has_more, rand_out))
            }
            SortAxis::None => {
                if cursor.is_some() {
                    return Err("Invalid order_by for cursor pagination: none".into());
                }
                let mut slots = Vec::with_capacity(limit + 1);
                for d in base_bm.iter() {
                    if self.doc_passes_inline(d, min_like, min_text, &matchers) {
                        slots.push(d);
                        if slots.len() > limit {
                            break;
                        }
                    }
                }
                let has_more = slots.len() > limit;
                slots.truncate(limit);
                Ok((slots, has_more, None))
            }
            SortAxis::Numeric(field) => {
                let cursor_int = match cursor {
                    Some(TrackCursor::Int(v, cid)) => {
                        let cmp_val = if field == crate::filter::NumericField::AbsId {
                            cid.unsigned_abs() as i128
                        } else {
                            *v as i128
                        };
                        Some((cmp_val, *cid))
                    }
                    None => None,
                    Some(_) => return Err("Expected integer/datetime cursor".into()),
                };

                let hot = field.hot_track();
                let use_top_k = match hot {
                    None => true, // Cold numeric fields never build full-corpus tracks
                    Some(h) => {
                        self.tracks_dirty[h as usize]
                            || (matches!(&mask, CandidateMask::Bitmap(_)) && base_bm.len() <= 4096)
                    }
                };

                if use_top_k {
                    let iter = base_bm.iter().filter_map(|doc| {
                        if !self.doc_passes_inline(doc, min_like, min_text, &matchers) {
                            return None;
                        }
                        if let Some((cval, cid)) = cursor_int {
                            if self.field_is_null(field, doc) {
                                return None;
                            }
                            let dval = self.numeric_val_i128(field, doc);
                            let did = self.doc_ids[doc as usize];
                            let ok = if descending {
                                (dval, did) < (cval, cid)
                            } else {
                                (dval, did) > (cval, cid)
                            };
                            if !ok {
                                return None;
                            }
                        }
                        Some(NumericRankItem {
                            descending,
                            key: self.numeric_key(field, doc),
                            novel_id: self.doc_ids[doc as usize],
                            doc,
                        })
                    });
                    let (hits, has_more) = collect_top_k(iter, limit);
                    return Ok((hits.into_iter().map(|h| h.doc).collect(), has_more, None));
                }

                let hot = hot.expect("hot track must exist when use_top_k is false");
                let track = &self.tracks[hot as usize];
                let mut out = Vec::with_capacity(limit + 1);
                if descending {
                    let start = match cursor_int {
                        None => 0,
                        Some((cval, cid)) => track.partition_point(|&doc| {
                            if self.field_is_null(field, doc) {
                                return true;
                            }
                            let dval = self.numeric_val_i128(field, doc);
                            let did = self.doc_ids[doc as usize];
                            dval > cval || (dval == cval && did >= cid)
                        }),
                    };
                    for &doc in &track[start..] {
                        if hot == HotTrack::Like
                            && min_like > 0
                            && (self.likes[doc as usize] as i64) < min_like
                        {
                            break;
                        }
                        if hot == HotTrack::Text
                            && min_text > 0
                            && (self.texts[doc as usize] as i64) < min_text
                        {
                            break;
                        }
                        if (matches!(mask, CandidateMask::AllAlive) || base_bm.contains(doc))
                            && self.doc_passes_inline(doc, min_like, min_text, &matchers)
                        {
                            out.push(doc);
                            if out.len() > limit {
                                break;
                            }
                        }
                    }
                } else {
                    let null_end = track.partition_point(|&doc| self.field_is_null(field, doc));
                    let threshold_end = match hot {
                        HotTrack::Like if min_like > 0 => {
                            self.min_bound_track_end(HotTrack::Like, min_like, 0)
                        }
                        HotTrack::Text if min_text > 0 => {
                            self.min_bound_track_end(HotTrack::Text, 0, min_text)
                        }
                        _ => track.len(),
                    };

                    match cursor_int {
                        Some((cval, cid)) => {
                            let cursor_end = track.partition_point(|&doc| {
                                if self.field_is_null(field, doc) {
                                    return true;
                                }
                                let dval = self.numeric_val_i128(field, doc);
                                let did = self.doc_ids[doc as usize];
                                dval > cval || (dval == cval && did > cid)
                            });
                            let end = cursor_end.min(threshold_end);
                            if null_end < end {
                                for &doc in track[null_end..end].iter().rev() {
                                    if (matches!(mask, CandidateMask::AllAlive)
                                        || base_bm.contains(doc))
                                        && self
                                            .doc_passes_inline(doc, min_like, min_text, &matchers)
                                    {
                                        out.push(doc);
                                        if out.len() > limit {
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                        None => 'asc_outer: {
                            if null_end < threshold_end {
                                for &doc in track[null_end..threshold_end].iter().rev() {
                                    if (matches!(mask, CandidateMask::AllAlive)
                                        || base_bm.contains(doc))
                                        && self
                                            .doc_passes_inline(doc, min_like, min_text, &matchers)
                                    {
                                        out.push(doc);
                                        if out.len() > limit {
                                            break 'asc_outer;
                                        }
                                    }
                                }
                            }
                            for &doc in track[..null_end].iter().rev() {
                                if (matches!(mask, CandidateMask::AllAlive)
                                    || base_bm.contains(doc))
                                    && self.doc_passes_inline(doc, min_like, min_text, &matchers)
                                {
                                    out.push(doc);
                                    if out.len() > limit {
                                        break 'asc_outer;
                                    }
                                }
                            }
                        }
                    }
                }
                let has_more = out.len() > limit;
                out.truncate(limit);
                Ok((out, has_more, None))
            }
            SortAxis::String(field) => {
                let cursor_str = match cursor {
                    Some(TrackCursor::Str(s, cid)) => Some((s.as_str(), *cid)),
                    None => None,
                    Some(_) => return Err("Expected string cursor".into()),
                };
                let bm = self.verify_all_matching(&mask, min_like, min_text, &matchers);
                let iter = bm.iter().filter_map(|doc| {
                    let (is_null, val) = self.string_val(field, doc);
                    let nid = self.doc_ids[doc as usize];
                    if let Some((cstr, cid)) = cursor_str {
                        if is_null {
                            return None;
                        }
                        let ok = if descending {
                            (val, nid) < (cstr, cid)
                        } else {
                            (val, nid) > (cstr, cid)
                        };
                        if !ok {
                            return None;
                        }
                    }
                    Some(StringRankItem {
                        descending,
                        is_null,
                        val,
                        novel_id: nid,
                        doc,
                    })
                });
                let (hits, has_more) = collect_top_k(iter, limit);
                Ok((hits.into_iter().map(|h| h.doc).collect(), has_more, None))
            }
        }
    }
}
