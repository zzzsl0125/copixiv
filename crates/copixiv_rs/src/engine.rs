use parking_lot::{RwLock, RwLockUpgradableReadGuard};

use crate::filter::{QueryFilter, SortAxis, TrackCursor};
use crate::model::{NovelRecord, QueryPage, TagSuggestItem};
use crate::query::mask::CandidateMask;
use crate::storage::EngineState;

/// Thread-safe in-process query engine combining novel search/pagination
/// and tag similarity suggestions behind a single `parking_lot::RwLock`.
pub struct NovelIndex {
    inner: RwLock<EngineState>,
}

impl Default for NovelIndex {
    fn default() -> Self {
        Self::new()
    }
}

impl NovelIndex {
    pub fn new() -> Self {
        Self {
            inner: RwLock::new(EngineState::default()),
        }
    }

    pub fn ready(&self) -> bool {
        self.inner.read().initialized
    }

    pub fn needs_rebuild(&self) -> bool {
        self.inner.read().needs_rebuild
    }

    pub fn mark_needs_rebuild(&self) {
        self.inner.write().needs_rebuild = true;
    }

    pub fn stats(&self) -> (usize, usize, usize, bool, usize, bool) {
        let s = self.inner.read();
        (
            s.alive.len() as usize,
            s.doc_ids.len(),
            s.blocked_bitmap.len() as usize,
            s.exclude_blocked_default,
            s.tag_index.tag_count(),
            s.needs_rebuild,
        )
    }

    pub fn memory_breakdown(&self) -> Vec<(String, usize, usize)> {
        self.inner.read().memory_breakdown()
    }

    pub fn swap_state(&self, next: EngineState) {
        let mut guard = self.inner.write();
        *guard = next;
    }

    pub fn upsert_batch(&self, rows: Vec<NovelRecord>) {
        self.inner.write().upsert_batch(rows);
    }

    pub fn delete_ids(&self, novel_ids: &[i64]) {
        self.inner.write().delete_ids(novel_ids);
    }

    pub fn toggle_special_follow(&self, author_id: i64, is_special_follow: bool) {
        self.inner
            .write()
            .toggle_special_follow(author_id, is_special_follow);
    }

    pub fn set_blocked_tags(&self, blocked_tags: Vec<String>) {
        self.inner.write().set_blocked_tags(blocked_tags);
    }

    pub fn set_exclude_blocked_default(&self, value: bool) {
        self.inner.write().exclude_blocked_default = value;
    }

    #[allow(clippy::too_many_arguments)]
    pub fn apply_delta(
        &self,
        delete_ids: &[i64],
        upsert_rows: Vec<NovelRecord>,
        blocked_tags: Option<Vec<String>>,
        exclude_blocked_default: Option<bool>,
        special_follow_changes: &[(i64, bool)],
        replace_all_tags: Option<Vec<(i64, String, i32)>>,
        delete_tag_names: Option<Vec<String>>,
        upsert_tags: Option<Vec<(i64, String, i32)>>,
        aliased_tag_ids: Option<Vec<i64>>,
    ) {
        self.inner.write().apply_delta(
            delete_ids,
            upsert_rows,
            blocked_tags,
            exclude_blocked_default,
            special_follow_changes,
            replace_all_tags,
            delete_tag_names,
            upsert_tags,
            aliased_tag_ids,
        );
    }

    pub fn query_novel_rows(
        &self,
        filter: &QueryFilter,
        axis: SortAxis,
        descending: bool,
        limit: usize,
        cursor: Option<&TrackCursor>,
    ) -> Result<QueryPage<NovelRecord>, String> {
        if let SortAxis::Numeric(field) = axis {
            let guard = self.inner.read();
            let (mask, matchers) = guard.resolve_candidates(filter);
            if mask.is_empty(&guard.alive) {
                return Ok((Vec::new(), false, None));
            }
            let hot = field.hot_track();
            let can_bypass_track = match hot {
                None => true,
                Some(h) => {
                    !guard.tracks_dirty[h as usize]
                        || matches!(&mask, CandidateMask::Bitmap(bm) if bm.len() <= 4096)
                }
            };
            if can_bypass_track {
                let (slots, has_more, rand_out) = guard.query_novels_slots_with_mask(
                    filter, mask, matchers, axis, descending, limit, cursor,
                )?;
                let rows = slots
                    .into_iter()
                    .map(|d| guard.materialize_doc(d))
                    .collect();
                return Ok((rows, has_more, rand_out));
            }
            drop(guard);

            // Large candidate set needs a dirty hot track: build off write lock under
            // `upgradable_read()` so concurrent readers stay unblocked during O(N log N) sort.
            let hot = hot.expect("hot track present when track rebuild requested");
            let up = self.inner.upgradable_read();
            let guard = if up.tracks_dirty[hot as usize] {
                let built = up.build_track(hot);
                let mut w = RwLockUpgradableReadGuard::upgrade(up);
                if w.tracks_dirty[hot as usize] {
                    w.tracks[hot as usize] = built;
                    w.tracks_dirty[hot as usize] = false;
                }
                parking_lot::RwLockWriteGuard::downgrade(w)
            } else {
                RwLockUpgradableReadGuard::downgrade(up)
            };

            let (mask, matchers) = guard.resolve_candidates(filter);
            let (slots, has_more, rand_out) = guard.query_novels_slots_with_mask(
                filter, mask, matchers, axis, descending, limit, cursor,
            )?;
            let rows = slots
                .into_iter()
                .map(|d| guard.materialize_doc(d))
                .collect();
            return Ok((rows, has_more, rand_out));
        }

        let guard = self.inner.read();
        let (mask, matchers) = guard.resolve_candidates(filter);
        let (slots, has_more, rand_out) = guard.query_novels_slots_with_mask(
            filter, mask, matchers, axis, descending, limit, cursor,
        )?;
        let rows = slots
            .into_iter()
            .map(|d| guard.materialize_doc(d))
            .collect();
        Ok((rows, has_more, rand_out))
    }

    pub fn suggest_tag_aliases(
        &self,
        limit: usize,
        offset: i64,
        target_tag: Option<&str>,
    ) -> (Vec<TagSuggestItem>, i64) {
        {
            let guard = self.inner.read();
            if !guard.tag_index.sorted_dirty {
                return guard.tag_index.suggest_aliases(limit, offset, target_tag);
            }
        }
        let up = self.inner.upgradable_read();
        let guard = if up.tag_index.sorted_dirty {
            let mut w = RwLockUpgradableReadGuard::upgrade(up);
            w.tag_index.ensure_sorted();
            parking_lot::RwLockWriteGuard::downgrade(w)
        } else {
            RwLockUpgradableReadGuard::downgrade(up)
        };
        guard.tag_index.suggest_aliases(limit, offset, target_tag)
    }

    pub fn get_novel_rows_by_ids(&self, novel_ids: &[i64]) -> Vec<NovelRecord> {
        self.inner.read().get_novel_rows_by_ids(novel_ids)
    }

    pub fn count_novels(&self, filter: &QueryFilter, cap: usize) -> (usize, bool) {
        self.inner.read().count_novels(filter, cap)
    }

    pub fn has_match(&self, filter: &QueryFilter) -> bool {
        self.inner.read().has_match(filter)
    }

    pub fn list_matching_ids(&self, filter: &QueryFilter) -> Vec<i64> {
        self.inner.read().list_matching_ids(filter)
    }

    pub fn list_blocked_ids(&self) -> Vec<i64> {
        self.inner.read().list_blocked_ids()
    }

    pub fn sort_ids(&self, novel_ids: &[i64], axis: SortAxis, descending: bool) -> Vec<i64> {
        self.inner.read().sort_ids(novel_ids, axis, descending)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::filter::{HotTrack, NumericField};
    use crate::random::hex16_decode;
    use crate::staging::StagingBuilder;
    use crate::text_index::build_search_lower;
    use rustc_hash::FxHashSet;

    fn sample_row(id: i64, title: &str, tags: Vec<&str>) -> NovelRecord {
        NovelRecord {
            id,
            title: title.to_string(),
            author_id: Some(10),
            author_name: Some("AuthorA".to_string()),
            series_id: None,
            series_name: None,
            series_index: None,
            text: 1000,
            like: id.abs() * 10,
            view: id.abs() * 100,
            has_epub: 0,
            create_time_us: Some(id.abs() * 1_000_000),
            source: "pixiv".to_string(),
            is_favourite: false,
            is_special_follow: false,
            tags: tags.into_iter().map(str::to_string).collect(),
        }
    }

    #[test]
    fn search_text_ascii_only_fold_and_whitespace() {
        let s = build_search_lower("Hello Ñ World", Some("Bob"), None, ["原神", "A"]);
        assert_eq!(s, "hello Ñ world bob 原神 a");

        let mut b = StagingBuilder::new(4);
        b.push_batch(vec![
            sample_row(1, "Hello Ñ Test", vec!["原神", "百合"]),
            sample_row(2, "ñ lower only", vec!["原神"]),
        ]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![], vec![], vec![]));

        let f = QueryFilter {
            keyword_segments: vec!["hello".into(), "Ñ".into()],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f), vec![1]);

        let f_1char = QueryFilter {
            keyword_segments: vec!["原".into()],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_1char), vec![1, 2]);
    }

    #[test]
    fn abs_id_time_axis_and_nulls_order() {
        let mut r1 = sample_row(-200, "Local newer", vec![]);
        r1.create_time_us = None;
        let mut r2 = sample_row(100, "Pixiv older", vec![]);
        r2.create_time_us = Some(500);
        let mut r3 = sample_row(300, "Pixiv newest", vec![]);
        r3.create_time_us = Some(900);
        let mut r4 = sample_row(200, "Pixiv tie abs 200", vec![]);
        r4.create_time_us = None;

        let mut b = StagingBuilder::new(4);
        b.push_batch(vec![r1, r2, r3, r4]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![], vec![], vec![]));

        let f = QueryFilter::default();
        let desc_ids: Vec<i64> = engine
            .query_novel_rows(&f, SortAxis::Numeric(NumericField::AbsId), true, 10, None)
            .unwrap()
            .0
            .into_iter()
            .map(|r| r.id)
            .collect();
        assert_eq!(desc_ids, vec![300, 200, -200, 100]);

        let asc_ids: Vec<i64> = engine
            .query_novel_rows(&f, SortAxis::Numeric(NumericField::AbsId), false, 10, None)
            .unwrap()
            .0
            .into_iter()
            .map(|r| r.id)
            .collect();
        assert_eq!(asc_ids, vec![100, -200, 200, 300]);

        let ct_desc: Vec<i64> = engine
            .query_novel_rows(
                &f,
                SortAxis::Numeric(NumericField::CreateTime),
                true,
                10,
                None,
            )
            .unwrap()
            .0
            .into_iter()
            .map(|r| r.id)
            .collect();
        assert_eq!(ct_desc, vec![200, -200, 300, 100]);

        let ct_asc: Vec<i64> = engine
            .query_novel_rows(
                &f,
                SortAxis::Numeric(NumericField::CreateTime),
                false,
                10,
                None,
            )
            .unwrap()
            .0
            .into_iter()
            .map(|r| r.id)
            .collect();
        assert_eq!(ct_asc, vec![100, 300, -200, 200]);

        let c = TrackCursor::Int(900, 300);
        let after_desc: Vec<i64> = engine
            .query_novel_rows(
                &f,
                SortAxis::Numeric(NumericField::CreateTime),
                true,
                10,
                Some(&c),
            )
            .unwrap()
            .0
            .into_iter()
            .map(|r| r.id)
            .collect();
        assert_eq!(after_desc, vec![100]);

        let c_asc = TrackCursor::Int(500, 100);
        let after_asc: Vec<i64> = engine
            .query_novel_rows(
                &f,
                SortAxis::Numeric(NumericField::CreateTime),
                false,
                10,
                Some(&c_asc),
            )
            .unwrap()
            .0
            .into_iter()
            .map(|r| r.id)
            .collect();
        assert_eq!(after_asc, vec![300]);
    }

    #[test]
    fn duplicate_delete_ids_and_random_pagination_and_early_stop_count() {
        let rows: Vec<NovelRecord> = (1..=35)
            .map(|i| sample_row(i, &format!("Book {i}"), vec!["T1"]))
            .collect();
        let mut b = StagingBuilder::new(35);
        b.push_batch(rows);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec!["T1".into()], true, vec![], vec![], vec![]));

        let f = QueryFilter::default();
        assert_eq!(engine.count_novels(&f, 5000), (0, false));
        engine.set_blocked_tags(vec![]);
        assert_eq!(engine.count_novels(&f, 5000), (35, false));

        // Early stop and binary-search count on clean Like track
        let f_min_like = QueryFilter {
            min_like: Some(200),
            ..Default::default()
        };
        assert_eq!(engine.count_novels(&f_min_like, 5000), (16, false));
        let asc_min_like: Vec<i64> = engine
            .query_novel_rows(
                &f_min_like,
                SortAxis::Numeric(NumericField::Like),
                false,
                3,
                None,
            )
            .unwrap()
            .0
            .into_iter()
            .map(|r| r.id)
            .collect();
        assert_eq!(asc_min_like, vec![20, 21, 22]);

        // Deleting duplicate IDs must not push duplicate slots into free_docs
        engine.delete_ids(&[2, 2, 2]);
        assert_eq!(engine.inner.read().free_docs.len(), 1);
        assert!(engine.inner.read().tracks_dirty[HotTrack::Like as usize]);

        // Unfiltered Like query lazily rebuilds the clean track under upgradable_read
        let _ = engine
            .query_novel_rows(&f, SortAxis::Numeric(NumericField::Like), true, 5, None)
            .unwrap();
        assert!(!engine.inner.read().tracks_dirty[HotTrack::Like as usize]);

        // Mutation on non-hot field keeps HotTrack::Like clean!
        let mut r3 = sample_row(3, "NonHotEdit", vec!["T2"]);
        r3.is_favourite = true;
        engine.upsert_batch(vec![r3]);
        assert!(!engine.inner.read().tracks_dirty[HotTrack::Like as usize]);

        // Exhaustive random walk collects remaining 34 IDs cleanly
        let mut seen = FxHashSet::default();
        let mut cur: Option<TrackCursor> = None;
        loop {
            let (page_rows, more, next_rand) = engine
                .query_novel_rows(&f, SortAxis::Random, true, 10, cur.as_ref())
                .unwrap();
            for row in page_rows {
                assert!(seen.insert(row.id));
            }
            if !more {
                break;
            }
            let (sh, hh, lid) = next_rand.unwrap();
            cur = Some(TrackCursor::Random(
                hex16_decode(&sh).unwrap(),
                hex16_decode(&hh).unwrap(),
                lid,
            ));
        }
        assert_eq!(seen.len(), 34);
    }

    #[test]
    fn small_candidate_numeric_query_skips_dirty_track_rebuild_and_apply_delta_syncs_tags() {
        let mut r1 = sample_row(1, "Alpha", vec!["unique_small"]);
        r1.series_index = Some(10);
        let r2 = sample_row(2, "Beta", vec!["other"]);
        let mut r3 = sample_row(3, "Gamma", vec!["unique_small"]);
        r3.series_index = Some(20);

        let mut b = StagingBuilder::new(8);
        b.push_batch(vec![r1, r2, r3]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(
            vec![],
            true,
            vec![],
            vec![(1, "unique_small".into(), 2), (2, "other".into(), 1)],
            vec![],
        ));

        // Force HotTrack::Like dirty via a novel like mutation
        let mut r2_mod = sample_row(2, "Beta", vec!["other"]);
        r2_mod.like = 99999;
        engine.upsert_batch(vec![r2_mod]);
        assert!(engine.inner.read().tracks_dirty[HotTrack::Like as usize]);

        // Small candidate set queries Like without rebuilding track
        let filter = QueryFilter {
            tags: vec!["unique_small".into()],
            ..Default::default()
        };
        let rows = engine
            .query_novel_rows(
                &filter,
                SortAxis::Numeric(NumericField::Like),
                true,
                10,
                None,
            )
            .unwrap()
            .0;
        assert_eq!(rows.iter().map(|r| r.id).collect::<Vec<_>>(), vec![3, 1]);
        assert!(engine.inner.read().tracks_dirty[HotTrack::Like as usize]);

        // Atomic delta syncs both novel index and tag index
        let row4 = sample_row(4, "Four", vec!["unique_small_v2"]);
        engine.apply_delta(
            &[],
            vec![row4],
            None,
            None,
            &[],
            None,
            None,
            Some(vec![(3, "unique_small_v2".into(), 1)]),
            None,
        );
        let (items, _) = engine.suggest_tag_aliases(5, 0, Some("unique_small"));
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].candidates[0].name, "unique_small_v2");
    }
}
