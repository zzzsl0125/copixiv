use parking_lot::{RwLock, RwLockUpgradableReadGuard};

use crate::filter::{QueryFilter, SortAxis, TrackCursor};
use crate::model::{NovelRecord, QueryPage, TagSuggestItem};
use crate::query::mask::CandidateMask;
use crate::storage::EngineState;
use crate::tag_index::TagIndex;

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
            s.tag_postings.iter().filter(|h| !h.is_empty()).count(),
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

    #[allow(clippy::too_many_arguments)]
    pub fn apply_delta(
        &self,
        delete_ids: &[i64],
        delete_series_ids: &[i64],
        upsert_rows: Vec<NovelRecord>,
        blocked_tags: Option<Vec<String>>,
        exclude_blocked_default: Option<bool>,
        special_follow_changes: &[(i64, bool)],
        aliased_tags: Option<Vec<String>>,
    ) {
        self.inner.write().apply_delta(
            delete_ids,
            delete_series_ids,
            upsert_rows,
            blocked_tags,
            exclude_blocked_default,
            special_follow_changes,
            aliased_tags,
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
            let can_bypass_track = !guard.tracks_dirty[hot as usize]
                || matches!(&mask, CandidateMask::Bitmap(bm) if bm.len() <= 4096);
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
    ) -> (Vec<TagSuggestItem>, Option<i64>) {
        {
            let guard = self.inner.read();
            if !guard.tag_index.dirty {
                return guard.tag_index.suggest_aliases(
                    &guard.tag_interner,
                    &guard.tag_postings,
                    &guard.postings_slab,
                    limit,
                    offset,
                    target_tag,
                );
            }
        }
        let up = self.inner.upgradable_read();
        let guard = if up.tag_index.dirty {
            let built = TagIndex::build_from_state(
                &up.tag_interner,
                &up.tag_postings,
                &up.postings_slab,
                &up.aliased_tag_names,
            );
            let mut w = RwLockUpgradableReadGuard::upgrade(up);
            if w.tag_index.dirty {
                w.tag_index = built;
            }
            parking_lot::RwLockWriteGuard::downgrade(w)
        } else {
            RwLockUpgradableReadGuard::downgrade(up)
        };
        guard.tag_index.suggest_aliases(
            &guard.tag_interner,
            &guard.tag_postings,
            &guard.postings_slab,
            limit,
            offset,
            target_tag,
        )
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

    pub fn sort_ids(&self, novel_ids: &[i64], axis: SortAxis, descending: bool) -> Vec<i64> {
        self.inner.read().sort_ids(novel_ids, axis, descending)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::filter::{HotTrack, IntFilterField, NumericField, StrFilterField};
    use crate::random::hex16_decode;
    use crate::staging::StagingBuilder;
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
            chapter_count: 1,
        }
    }

    fn custom_row(
        id: i64,
        source: &str,
        author_id: Option<i64>,
        series_id: Option<i64>,
        title: &str,
        tags: Vec<&str>,
    ) -> NovelRecord {
        let mut row = sample_row(id, title, tags);
        row.source = source.to_string();
        row.author_id = author_id;
        row.author_name = author_id.map(|aid| format!("Author{aid}"));
        row.series_id = series_id;
        row.series_name = series_id.map(|sid| format!("Series{sid}"));
        row.series_index = series_id.map(|_| 1);
        row
    }

    fn paginate_abs_id(
        engine: &NovelIndex,
        filter: &QueryFilter,
        descending: bool,
        limit: usize,
    ) -> Vec<i64> {
        let mut collected = Vec::new();
        let mut seen = FxHashSet::default();
        let mut cursor: Option<TrackCursor> = None;
        loop {
            let (rows, has_more, rand_out) = engine
                .query_novel_rows(
                    filter,
                    SortAxis::Numeric(NumericField::AbsId),
                    descending,
                    limit,
                    cursor.as_ref(),
                )
                .unwrap();
            assert!(rand_out.is_none());
            if has_more {
                assert_eq!(rows.len(), limit);
            } else {
                assert!(rows.len() <= limit);
            }
            for row in &rows {
                assert!(
                    seen.insert(row.id),
                    "duplicate id {} across AbsId page boundary",
                    row.id
                );
                collected.push(row.id);
            }
            if !has_more {
                break;
            }
            let last = rows.last().expect("has_more implies non-empty page");
            cursor = Some(TrackCursor::Int(Some(last.id), last.id));
        }
        collected
    }

    #[test]
    fn search_text_ascii_only_fold_and_1char() {
        let mut b = StagingBuilder::new(4);
        b.push_batch(vec![
            sample_row(1, "Hello Ñ Test", vec!["原神", "百合"]),
            sample_row(2, "ñ lower only", vec!["原神"]),
        ]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![]));

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
        engine.swap_state(b.finalize(vec![], true, vec![]));

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

        let c = TrackCursor::Int(Some(900), 300);
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

        let c_asc = TrackCursor::Int(Some(500), 100);
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
        assert_eq!(after_asc, vec![300, -200, 200]);

        let c_null_desc = TrackCursor::Int(None, 200);
        let after_null_desc: Vec<i64> = engine
            .query_novel_rows(
                &f,
                SortAxis::Numeric(NumericField::CreateTime),
                true,
                10,
                Some(&c_null_desc),
            )
            .unwrap()
            .0
            .into_iter()
            .map(|r| r.id)
            .collect();
        assert_eq!(after_null_desc, vec![-200, 300, 100]);

        let c_null_asc = TrackCursor::Int(None, -200);
        let after_null_asc: Vec<i64> = engine
            .query_novel_rows(
                &f,
                SortAxis::Numeric(NumericField::CreateTime),
                false,
                10,
                Some(&c_null_asc),
            )
            .unwrap()
            .0
            .into_iter()
            .map(|r| r.id)
            .collect();
        assert_eq!(after_null_asc, vec![200]);
    }

    #[test]
    fn duplicate_delete_ids_and_random_pagination_and_early_stop_count() {
        let rows: Vec<NovelRecord> = (1..=35)
            .map(|i| sample_row(i, &format!("Book {i}"), vec!["T1"]))
            .collect();
        let mut b = StagingBuilder::new(35);
        b.push_batch(rows);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec!["T1".into()], true, vec![]));

        let f = QueryFilter::default();
        assert_eq!(engine.count_novels(&f, 5000), (0, false));
        engine.apply_delta(&[], &[], vec![], Some(vec![]), None, &[], None);
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
        engine.apply_delta(&[2, 2, 2], &[], vec![], None, None, &[], None);
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
        engine.apply_delta(&[], &[], vec![r3], None, None, &[], None);
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
        engine.swap_state(b.finalize(vec![], true, vec![]));
        assert!(engine.inner.read().tag_index.dirty);

        // Force HotTrack::Like dirty via a novel like mutation
        let mut r2_mod = sample_row(2, "Beta", vec!["other"]);
        r2_mod.like = 99999;
        engine.apply_delta(&[], &[], vec![r2_mod], None, None, &[], None);
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

        // Atomic delta syncs novel tags and derives TagIndex lazily on demand
        let row4 = sample_row(4, "Four", vec!["unique_small_v2"]);
        engine.apply_delta(&[], &[], vec![row4], None, None, &[], None);
        let (items, _) = engine.suggest_tag_aliases(5, 0, Some("unique_small"));
        assert!(!engine.inner.read().tag_index.dirty);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].target.reference_count, 2);
        assert!(items[0].target.id > 0);
        assert_eq!(items[0].candidates[0].name, "unique_small_v2");
        assert_eq!(items[0].candidates[0].reference_count, 1);

        // Alias update refreshes exclusions without rebuilding TagIndex when tags didn't change
        engine.apply_delta(
            &[],
            &[],
            vec![],
            None,
            None,
            &[],
            Some(vec!["unique_small_v2".into()]),
        );
        assert!(!engine.inner.read().tag_index.dirty);
        assert!(engine
            .suggest_tag_aliases(5, 0, Some("unique_small"))
            .0
            .is_empty());
    }

    #[test]
    fn non_pixiv_sources_interned_and_filtered() {
        assert_eq!(StrFilterField::parse("source"), Ok(StrFilterField::Source));

        let mut b = StagingBuilder::new(8);
        b.push_batch(vec![
            custom_row(10, "pixiv", Some(1), None, "Pixiv Story", vec!["isekai"]),
            custom_row(20, "local", Some(2), None, "Local Story", vec!["isekai"]),
            custom_row(
                -30,
                "esj",
                Some(-3),
                Some(-30),
                "ESJ Dragon",
                vec!["isekai", "magic"],
            ),
            custom_row(-40, "esj", Some(-4), None, "ESJ Slice", vec!["slice"]),
            custom_row(
                -50,
                "ao3",
                Some(-5),
                Some(-50),
                "AO3 Dragon",
                vec!["fantasy", "isekai"],
            ),
            custom_row(
                -60,
                "ao3",
                Some(-6),
                None,
                "AO3 Blocked",
                vec!["blocked_tag"],
            ),
        ]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec!["blocked_tag".into()], true, vec![]));

        // Verify source_interner & local_bitmap invariants for arbitrary non-Pixiv sources.
        {
            let state = engine.inner.read();
            assert_eq!(state.source_interner.len(), 4);
            let esj_sid = state.source_interner.get_id("esj").unwrap();
            let ao3_sid = state.source_interner.get_id("ao3").unwrap();
            assert_ne!(esj_sid, ao3_sid);

            let doc_esj = *state.id_to_doc.get(&-30).unwrap();
            let doc_ao3 = *state.id_to_doc.get(&-50).unwrap();
            let doc_local = *state.id_to_doc.get(&20).unwrap();
            assert_eq!(
                state
                    .source_interner
                    .get_str(state.doc_source[doc_esj as usize]),
                "esj"
            );
            assert_eq!(
                state
                    .source_interner
                    .get_str(state.doc_source[doc_ao3 as usize]),
                "ao3"
            );
            assert!(!state.local_bitmap.contains(doc_esj));
            assert!(!state.local_bitmap.contains(doc_ao3));
            assert!(state.local_bitmap.contains(doc_local));
        }

        // Single-source filters: source:esj;, source:ao3;, source:pixiv;, source:local;, unknown
        let f_esj = QueryFilter {
            str_filters: vec![(StrFilterField::Source, "esj".into())],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_esj), vec![-40, -30]);
        assert_eq!(engine.count_novels(&f_esj, 100), (2, false));
        assert!(engine.has_match(&f_esj));
        let esj_rows = engine
            .query_novel_rows(
                &f_esj,
                SortAxis::Numeric(NumericField::AbsId),
                true,
                10,
                None,
            )
            .unwrap()
            .0;
        assert_eq!(
            esj_rows.iter().map(|r| r.id).collect::<Vec<_>>(),
            vec![-40, -30]
        );
        assert!(esj_rows.iter().all(|r| r.source == "esj"));

        let f_ao3 = QueryFilter {
            str_filters: vec![(StrFilterField::Source, "ao3".into())],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_ao3), vec![-50]);
        assert_eq!(engine.count_novels(&f_ao3, 100), (1, false));
        let f_ao3_unblocked = QueryFilter {
            exclude_blocked_tags: Some(false),
            ..f_ao3.clone()
        };
        assert_eq!(engine.list_matching_ids(&f_ao3_unblocked), vec![-60, -50]);
        let ao3_rows = engine
            .query_novel_rows(
                &f_ao3_unblocked,
                SortAxis::Numeric(NumericField::AbsId),
                false,
                10,
                None,
            )
            .unwrap()
            .0;
        assert_eq!(
            ao3_rows.iter().map(|r| r.id).collect::<Vec<_>>(),
            vec![-50, -60]
        );
        assert!(ao3_rows.iter().all(|r| r.source == "ao3"));

        // With >2 sources present, source:pixiv; must NOT match esj/ao3 rows.
        let f_pixiv = QueryFilter {
            str_filters: vec![(StrFilterField::Source, "pixiv".into())],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_pixiv), vec![10]);

        let f_local = QueryFilter {
            str_filters: vec![(StrFilterField::Source, "local".into())],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_local), vec![20]);

        let f_missing = QueryFilter {
            str_filters: vec![(StrFilterField::Source, "syosetsu".into())],
            ..Default::default()
        };
        assert!(engine.list_matching_ids(&f_missing).is_empty());
        assert_eq!(engine.count_novels(&f_missing, 100), (0, false));
        assert!(!engine.has_match(&f_missing));

        // Compound queries: `source:esj; isekai` and `source:ao3; isekai dragon`
        let f_esj_tag = QueryFilter {
            str_filters: vec![(StrFilterField::Source, "esj".into())],
            tags: vec!["isekai".into()],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_esj_tag), vec![-30]);
        assert_eq!(engine.count_novels(&f_esj_tag, 100), (1, false));

        let f_ao3_compound = QueryFilter {
            str_filters: vec![(StrFilterField::Source, "ao3".into())],
            tags: vec!["fantasy".into()],
            keyword_segments: vec!["dragon".into()],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_ao3_compound), vec![-50]);

        // Two-source edge cases around planner's <=2 source_interner branch:
        // Case A: only "local" and "esj" (no "pixiv" interned) -> source:pixiv; must be empty.
        let mut b_local_esj = StagingBuilder::new(2);
        b_local_esj.push_batch(vec![
            custom_row(1, "local", Some(1), None, "Local", vec![]),
            custom_row(-2, "esj", Some(-2), None, "ESJ", vec![]),
        ]);
        let eng_local_esj = NovelIndex::new();
        eng_local_esj.swap_state(b_local_esj.finalize(vec![], true, vec![]));
        assert!(eng_local_esj.list_matching_ids(&f_pixiv).is_empty());
        assert_eq!(eng_local_esj.list_matching_ids(&f_esj), vec![-2]);
        assert_eq!(eng_local_esj.list_matching_ids(&f_local), vec![1]);

        // Case B: only "pixiv" and "esj" (no "local" interned) -> source:pixiv; must exclude esj.
        let mut b_pixiv_esj = StagingBuilder::new(2);
        b_pixiv_esj.push_batch(vec![
            custom_row(1, "pixiv", Some(1), None, "Pixiv", vec![]),
            custom_row(-2, "esj", Some(-2), None, "ESJ", vec![]),
        ]);
        let eng_pixiv_esj = NovelIndex::new();
        eng_pixiv_esj.swap_state(b_pixiv_esj.finalize(vec![], true, vec![]));
        assert_eq!(eng_pixiv_esj.list_matching_ids(&f_pixiv), vec![1]);
        assert_eq!(eng_pixiv_esj.list_matching_ids(&f_esj), vec![-2]);
    }

    #[test]
    fn negative_ids_filter_count_and_hydrate() {
        let large_neg_novel = -9_000_000_001_i64;
        let large_neg_author = -9_000_000_002_i64;
        let large_neg_series = -9_000_000_003_i64;

        let mut r1 = custom_row(-10, "esj", Some(-100), Some(-300), "Neg One", vec!["t_neg"]);
        r1.series_index = Some(1);
        r1.is_special_follow = true;

        let mut r2 = custom_row(
            -20,
            "esj",
            Some(-100),
            Some(-300),
            "Neg Two",
            vec!["t_neg", "t_two"],
        );
        r2.series_index = Some(2);
        r2.is_special_follow = true;

        let mut r3 = custom_row(
            -30,
            "ao3",
            Some(-100),
            Some(-400),
            "Neg Three",
            vec!["t_neg"],
        );
        r3.series_index = Some(1);
        r3.is_special_follow = true;

        let mut r4 = custom_row(
            large_neg_novel,
            "ao3",
            Some(large_neg_author),
            Some(large_neg_series),
            "Large Neg",
            vec!["t_big"],
        );
        r4.series_index = Some(7);

        // Positive decoy with same magnitudes (+100/+300) and a NULL author/series decoy.
        let r_pos_decoy = custom_row(
            10,
            "pixiv",
            Some(100),
            Some(300),
            "Pos Decoy",
            vec!["t_pos"],
        );
        let r_null_decoy = custom_row(-40, "esj", None, None, "Null Author Series", vec![]);

        let mut b = StagingBuilder::new(8);
        b.push_batch(vec![r1, r2, r3, r4, r_pos_decoy, r_null_decoy]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![]));

        // Filter by negative author_id
        let f_author = QueryFilter {
            int_filters: vec![(IntFilterField::AuthorId, -100)],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_author), vec![-30, -20, -10]);
        assert_eq!(engine.count_novels(&f_author, 100), (3, false));
        assert!(engine.has_match(&f_author));

        // Filter by negative series_id
        let f_series = QueryFilter {
            int_filters: vec![(IntFilterField::SeriesId, -300)],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_series), vec![-20, -10]);
        assert_eq!(engine.count_novels(&f_series, 100), (2, false));

        // Hydrate filtered query rows and verify negative IDs preserved
        let series_rows = engine
            .query_novel_rows(
                &f_series,
                SortAxis::Numeric(NumericField::AbsId),
                false,
                10,
                None,
            )
            .unwrap()
            .0;
        assert_eq!(series_rows.len(), 2);
        assert_eq!(series_rows[0].id, -10);
        assert_eq!(series_rows[0].author_id, Some(-100));
        assert_eq!(series_rows[0].series_id, Some(-300));
        assert_eq!(series_rows[0].series_index, Some(1));
        assert_eq!(series_rows[0].source, "esj");
        assert_eq!(series_rows[1].id, -20);
        assert_eq!(series_rows[1].series_index, Some(2));

        // Large 64-bit negative IDs across novel_id, author_id, series_id
        let f_big = QueryFilter {
            int_filters: vec![
                (IntFilterField::AuthorId, large_neg_author),
                (IntFilterField::SeriesId, large_neg_series),
            ],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_big), vec![large_neg_novel]);
        assert_eq!(engine.count_novels(&f_big, 10), (1, false));

        let f_neg_id = QueryFilter {
            int_filters: vec![(IntFilterField::Id, large_neg_novel)],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_neg_id), vec![large_neg_novel]);

        // Special follow filtering on negative author_id
        let f_sf = QueryFilter {
            only_special_follow: true,
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_sf), vec![-30, -20, -10]);

        // Hydrate directly via get_novel_rows_by_ids (missing negative ID skipped cleanly)
        let hydrated = engine.get_novel_rows_by_ids(&[large_neg_novel, -999_999, -40, 10]);
        assert_eq!(hydrated.len(), 3);
        assert_eq!(hydrated[0].id, large_neg_novel);
        assert_eq!(hydrated[0].author_id, Some(large_neg_author));
        assert_eq!(hydrated[0].series_id, Some(large_neg_series));
        assert_eq!(hydrated[0].source, "ao3");
        assert_eq!(hydrated[0].tags, vec!["t_big".to_string()]);
        assert_eq!(hydrated[1].id, -40);
        assert_eq!(hydrated[1].author_id, None);
        assert_eq!(hydrated[1].series_id, None);
        assert_eq!(hydrated[2].id, 10);
        assert_eq!(hydrated[2].author_id, Some(100));
    }

    #[test]
    fn abs_id_interleaved_cursor_pagination_desc_and_asc() {
        assert_eq!(
            SortAxis::parse("id"),
            Ok(SortAxis::Numeric(NumericField::AbsId))
        );

        // Mixed positive/negative IDs with interleaved magnitudes and tied abs(id) pairs (+25/-25, +75/-75).
        let spec_rows = vec![
            (-5, "esj", vec!["multi"]),
            (10, "pixiv", vec![]),
            (-25, "esj", vec!["multi"]),
            (25, "pixiv", vec!["multi"]),
            (-40, "ao3", vec![]),
            (60, "local", vec!["multi"]),
            (-75, "esj", vec!["multi"]),
            (75, "pixiv", vec!["multi"]),
            (-90, "ao3", vec![]),
            (100, "pixiv", vec![]),
            (-120, "esj", vec!["multi"]),
        ];
        let mut b = StagingBuilder::new(spec_rows.len());
        b.push_batch(
            spec_rows
                .into_iter()
                .map(|(id, src, tags)| {
                    custom_row(id, src, Some(id * 10), None, &format!("N{id}"), tags)
                })
                .collect(),
        );
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![]));

        let expected_desc = vec![-120, 100, -90, 75, -75, 60, -40, 25, -25, 10, -5];
        let expected_asc = vec![-5, 10, -25, 25, -40, 60, -75, 75, -90, 100, -120];
        let expected_filtered_desc = vec![-120, 75, -75, 60, 25, -25, -5];
        let expected_filtered_asc = vec![-5, -25, 25, 60, -75, 75, -120];

        // 1) Clean AllAlive pre-sorted track path across page sizes splitting ties and boundaries
        let f_all = QueryFilter::default();
        for limit in [1, 2, 3, 4, 5, 11, 20] {
            assert_eq!(paginate_abs_id(&engine, &f_all, true, limit), expected_desc);
            assert_eq!(paginate_abs_id(&engine, &f_all, false, limit), expected_asc);
        }

        // 2) CandidateMask::Bitmap path (top-k heap cursor path) across multiple page sizes
        let f_multi = QueryFilter {
            tags: vec!["multi".into()],
            ..Default::default()
        };
        for limit in [1, 2, 3, 4, 10] {
            assert_eq!(
                paginate_abs_id(&engine, &f_multi, true, limit),
                expected_filtered_desc
            );
            assert_eq!(
                paginate_abs_id(&engine, &f_multi, false, limit),
                expected_filtered_asc
            );
        }

        // 3) sort_ids parity on shuffled positive/negative IDs
        let shuffled = vec![-25, 100, -5, 75, -120, 10, -75, 25, -90, 60, -40];
        assert_eq!(
            engine.sort_ids(&shuffled, SortAxis::Numeric(NumericField::AbsId), true),
            expected_desc
        );
        assert_eq!(
            engine.sort_ids(&shuffled, SortAxis::Numeric(NumericField::AbsId), false),
            expected_asc
        );

        // 4) Dirty AbsId track: insert -80 (esj, "multi") and delete 100, then verify both
        //    filtered (dirty top-k path) and unfiltered (triggers lazy AbsId track rebuild).
        let row_neg80 = custom_row(-80, "esj", Some(-800), None, "N-80", vec!["multi"]);
        engine.apply_delta(&[100], &[], vec![row_neg80], None, None, &[], None);
        assert!(engine.inner.read().tracks_dirty[HotTrack::AbsId as usize]);

        assert_eq!(
            paginate_abs_id(&engine, &f_multi, true, 2),
            vec![-120, -80, 75, -75, 60, 25, -25, -5]
        );
        assert_eq!(
            paginate_abs_id(&engine, &f_multi, false, 2),
            vec![-5, -25, 25, 60, -75, 75, -80, -120]
        );
        assert!(engine.inner.read().tracks_dirty[HotTrack::AbsId as usize]);

        assert_eq!(
            paginate_abs_id(&engine, &f_all, true, 3),
            vec![-120, -90, -80, 75, -75, 60, -40, 25, -25, 10, -5]
        );
        assert!(!engine.inner.read().tracks_dirty[HotTrack::AbsId as usize]);
        assert_eq!(
            paginate_abs_id(&engine, &f_all, false, 3),
            vec![-5, 10, -25, 25, -40, 60, -75, 75, -80, -90, -120]
        );
    }
}
