use roaring::RoaringBitmap;
use rustc_hash::FxHashMap;
use smallvec::SmallVec;

use crate::filter::HotTrack;
use crate::interner::StringInterner;
use crate::model::NovelRecord;
use crate::postings::{PostingHandle, PostingSlab};
use crate::storage::EngineState;
use crate::text_index::collect_str_char_bigrams;

#[inline]
fn insert_posting_vec(
    interner: &mut StringInterner,
    postings: &mut Vec<PostingHandle>,
    slab: &mut PostingSlab,
    value: &str,
    doc: u32,
) -> u32 {
    let id = interner.intern(value);
    if id as usize >= postings.len() {
        postings.resize(id as usize + 1, PostingHandle::EMPTY);
    }
    slab.insert(&mut postings[id as usize], doc);
    id
}

#[inline]
fn insert_bm(bm: &mut RoaringBitmap, doc: u32, sequential: bool) {
    if sequential {
        if !bm.push(doc) {
            bm.insert(doc);
        }
    } else {
        bm.insert(doc);
    }
}

impl EngineState {
    pub fn push_empty_doc(&mut self, novel_id: i64) -> u32 {
        let doc = self.doc_ids.len() as u32;
        self.id_to_doc.insert(novel_id, doc);
        self.doc_ids.push(novel_id);
        self.likes.push(0);
        self.views.push(0);
        self.texts.push(0);
        self.has_epub.push(0);
        self.chapter_counts.push(1);
        self.create_time_us.push(0);
        self.author_id.push(0);
        self.series_id.push(0);
        self.series_index.push(0);
        self.doc_tags.ensure_slot(doc);
        self.doc_source.push(StringInterner::NONE);
        self.doc_author_name.push(StringInterner::NONE);
        self.doc_series_name.push(StringInterner::NONE);
        doc
    }

    fn alloc_doc(&mut self, novel_id: i64) -> (u32, bool) {
        if let Some(&doc) = self.id_to_doc.get(&novel_id) {
            return (doc, false);
        }
        if let Some(doc) = self.free_docs.pop() {
            self.id_to_doc.insert(novel_id, doc);
            self.doc_ids[doc as usize] = novel_id;
            return (doc, true);
        }
        (self.push_empty_doc(novel_id), true)
    }

    fn clear_doc_postings(&mut self, doc: u32, bigram_scratch: &mut Vec<u64>, for_delete: bool) {
        let idx = doc as usize;

        // Collect existing title char 2-grams before mutating title arena.
        let old_title_str = self.title_arena.get_str(doc);
        if !old_title_str.is_empty() {
            collect_str_char_bigrams(old_title_str, bigram_scratch);
            if !bigram_scratch.is_empty() {
                self.text_index.remove_doc(doc, bigram_scratch);
            }
        }

        self.fav_bitmap.remove(doc);
        self.sf_bitmap.remove(doc);
        self.local_bitmap.remove(doc);
        self.blocked_bitmap.remove(doc);

        self.null_create_time.remove(doc);
        if !self.null_author_id.remove(doc) {
            let aid = self.author_id[idx];
            if let Some(handle) = self.author_postings.get_mut(&aid) {
                self.postings_slab.remove(handle, doc);
                if handle.is_empty() {
                    self.author_postings.remove(&aid);
                }
            }
        }
        if !self.null_series_id.remove(doc) {
            let sid = self.series_id[idx];
            if let Some(handle) = self.series_postings.get_mut(&sid) {
                self.postings_slab.remove(handle, doc);
                if handle.is_empty() {
                    self.series_postings.remove(&sid);
                }
            }
        }
        self.null_series_index.remove(doc);

        self.doc_source[idx] = StringInterner::NONE;
        let old_an_id = self.doc_author_name[idx];
        if old_an_id != StringInterner::NONE {
            self.postings_slab
                .remove(&mut self.author_name_postings[old_an_id as usize], doc);
            self.doc_author_name[idx] = StringInterner::NONE;
        }
        let old_sn_id = self.doc_series_name[idx];
        if old_sn_id != StringInterner::NONE {
            self.postings_slab
                .remove(&mut self.series_name_postings[old_sn_id as usize], doc);
            self.doc_series_name[idx] = StringInterner::NONE;
        }

        for &tid in self.doc_tags.get(doc) {
            self.postings_slab
                .remove(&mut self.tag_postings[tid as usize], doc);
        }
        self.doc_tags.clear_slot(doc);

        if for_delete {
            self.title_arena.clear_slot(doc);
        }
    }

    pub fn write_row_into_doc(
        &mut self,
        doc: u32,
        row: &NovelRecord,
        sequential_push: bool,
        bigram_scratch: &mut Vec<u64>,
        tag_ids_scratch: &mut Vec<u32>,
    ) {
        let idx = doc as usize;
        if sequential_push {
            self.alive.push(doc);
        } else {
            self.alive.insert(doc);
        }

        self.likes[idx] = Self::clamp_i32(row.like);
        self.views[idx] = Self::clamp_i32(row.view);
        self.texts[idx] = Self::clamp_i32(row.text);
        self.has_epub[idx] = Self::clamp_u8(row.has_epub);
        self.chapter_counts[idx] = row.chapter_count.max(1);

        match row.create_time_us {
            Some(us) => self.create_time_us[idx] = us,
            None => {
                self.create_time_us[idx] = 0;
                insert_bm(&mut self.null_create_time, doc, sequential_push);
            }
        }

        match row.author_id {
            Some(aid) => {
                self.author_id[idx] = aid;
                let handle = self.author_postings.entry(aid).or_default();
                self.postings_slab.insert(handle, doc);
            }
            None => {
                self.author_id[idx] = 0;
                insert_bm(&mut self.null_author_id, doc, sequential_push);
            }
        }

        match row.series_id {
            Some(sid) => {
                self.series_id[idx] = sid;
                let handle = self.series_postings.entry(sid).or_default();
                self.postings_slab.insert(handle, doc);
            }
            None => {
                self.series_id[idx] = 0;
                insert_bm(&mut self.null_series_id, doc, sequential_push);
            }
        }

        match row.series_index {
            Some(sidx) => self.series_index[idx] = Self::clamp_i32(sidx),
            None => {
                self.series_index[idx] = 0;
                insert_bm(&mut self.null_series_index, doc, sequential_push);
            }
        }

        if row.is_favourite {
            insert_bm(&mut self.fav_bitmap, doc, sequential_push);
        }

        if row.is_special_follow {
            insert_bm(&mut self.sf_bitmap, doc, sequential_push);
        }

        let src_id = self.source_interner.intern(&row.source);
        self.doc_source[idx] = src_id;
        if row.source == "local" {
            insert_bm(&mut self.local_bitmap, doc, sequential_push);
        }

        if let Some(ref an) = row.author_name {
            let an_id = insert_posting_vec(
                &mut self.author_name_interner,
                &mut self.author_name_postings,
                &mut self.postings_slab,
                an,
                doc,
            );
            self.doc_author_name[idx] = an_id;
        } else {
            self.doc_author_name[idx] = StringInterner::NONE;
        }

        if let Some(ref sn) = row.series_name {
            let sn_id = insert_posting_vec(
                &mut self.series_name_interner,
                &mut self.series_name_postings,
                &mut self.postings_slab,
                sn,
                doc,
            );
            self.doc_series_name[idx] = sn_id;
        } else {
            self.doc_series_name[idx] = StringInterner::NONE;
        }

        let mut is_blocked = false;
        tag_ids_scratch.clear();
        for tag in &row.tags {
            let tid = insert_posting_vec(
                &mut self.tag_interner,
                &mut self.tag_postings,
                &mut self.postings_slab,
                tag,
                doc,
            );
            if !tag_ids_scratch.contains(&tid) {
                tag_ids_scratch.push(tid);
            }
            if !sequential_push && !self.blocked_tag_names.is_empty() && self.is_tag_blocked(tag) {
                is_blocked = true;
            }
        }
        self.doc_tags.set(doc, tag_ids_scratch);
        if is_blocked {
            self.blocked_bitmap.insert(doc);
        }

        self.title_arena.set_str(doc, &row.title);

        collect_str_char_bigrams(&row.title, bigram_scratch);
        self.text_index.insert_doc(doc, bigram_scratch);
    }

    #[inline]
    fn is_tag_blocked(&self, tag: &str) -> bool {
        if tag.bytes().any(|b| b.is_ascii_uppercase()) {
            self.blocked_tag_names.contains(&tag.to_ascii_lowercase())
        } else {
            self.blocked_tag_names.contains(tag)
        }
    }

    pub fn recompute_blocked_bitmap(&mut self) {
        self.blocked_bitmap.clear();
        if self.blocked_tag_names.is_empty() {
            return;
        }
        self.blocked_tag_names = std::mem::take(&mut self.blocked_tag_names)
            .into_iter()
            .map(|name| name.to_ascii_lowercase())
            .collect();
        for (tid, handle) in self.tag_postings.iter().enumerate() {
            if !handle.is_empty() && self.is_tag_blocked(self.tag_interner.get_str(tid as u32)) {
                handle.union_into(&self.postings_slab, &mut self.blocked_bitmap);
            }
        }
    }

    fn diff_hot_mask(&self, doc: u32, row: &NovelRecord) -> u8 {
        let idx = doc as usize;
        let mut mask = 0u8;
        if (
            self.null_create_time.contains(doc),
            self.create_time_us[idx],
        ) != (
            row.create_time_us.is_none(),
            row.create_time_us.unwrap_or(0),
        ) {
            mask |= HotTrack::CreateTime.bit();
        }
        if self.likes[idx] != Self::clamp_i32(row.like) {
            mask |= HotTrack::Like.bit();
        }
        if self.views[idx] != Self::clamp_i32(row.view) {
            mask |= HotTrack::View.bit();
        }
        if self.texts[idx] != Self::clamp_i32(row.text) {
            mask |= HotTrack::Text.bit();
        }
        mask
    }

    pub fn delete_ids(&mut self, novel_ids: &[i64]) -> bool {
        let mut docs: Vec<u32> = novel_ids
            .iter()
            .filter_map(|nid| self.id_to_doc.get(nid).copied())
            .filter(|&doc| self.alive.contains(doc))
            .collect();
        docs.sort_unstable();
        docs.dedup();
        if docs.is_empty() {
            return false;
        }

        // O(1) track invalidation instead of O(N) Vec::remove inside the write lock.
        self.invalidate_hot_tracks(u8::MAX);

        let mut tags_affected = false;
        let mut bigram_scratch = Vec::with_capacity(256);
        for doc in docs {
            if !tags_affected && !self.doc_tags.get(doc).is_empty() {
                tags_affected = true;
            }
            let nid = self.doc_ids[doc as usize];
            self.clear_doc_postings(doc, &mut bigram_scratch, true);
            self.alive.remove(doc);
            self.id_to_doc.remove(&nid);
            self.free_docs.push(doc);
        }
        self.title_arena.maybe_compact();
        self.doc_tags.maybe_compact();
        self.maybe_compact_postings();
        self.text_index.maybe_compact();
        tags_affected
    }

    pub fn delete_series_ids(&mut self, series_ids: &[i64]) -> bool {
        if series_ids.is_empty() {
            return false;
        }
        let mut victim_ids: Vec<i64> = Vec::new();
        for &sid in series_ids {
            if let Some(&handle) = self.series_postings.get(&sid) {
                let bm = handle.to_bitmap(&self.postings_slab);
                for doc in bm.iter() {
                    if self.alive.contains(doc) {
                        victim_ids.push(self.doc_ids[doc as usize]);
                    }
                }
            }
        }
        if victim_ids.is_empty() {
            return false;
        }
        self.delete_ids(&victim_ids)
    }

    pub fn upsert_batch(&mut self, rows: Vec<NovelRecord>) -> bool {
        if rows.is_empty() {
            return false;
        }
        let mut dedup: Vec<NovelRecord> = Vec::with_capacity(rows.len());
        let mut seen: FxHashMap<i64, usize> =
            FxHashMap::with_capacity_and_hasher(rows.len(), Default::default());
        for row in rows {
            if let Some(&pos) = seen.get(&row.id) {
                dedup[pos] = row;
            } else {
                seen.insert(row.id, dedup.len());
                dedup.push(row);
            }
        }

        let mut tags_changed = false;

        let mut dirty_mask = 0u8;
        for row in &dedup {
            if let Some(&doc) = self.id_to_doc.get(&row.id) {
                dirty_mask |= self.diff_hot_mask(doc, row);
            } else {
                dirty_mask = u8::MAX;
                break;
            }
        }
        if dirty_mask != 0 {
            self.invalidate_hot_tracks(dirty_mask);
        }

        let mut old_tags_scratch: SmallVec<[u32; 16]> = SmallVec::new();
        let mut bigram_scratch = Vec::with_capacity(256);
        let mut tag_ids_scratch = Vec::with_capacity(32);
        for row in &dedup {
            let (doc, is_new) = self.alloc_doc(row.id);
            if !is_new {
                if !tags_changed {
                    old_tags_scratch.clear();
                    old_tags_scratch.extend_from_slice(self.doc_tags.get(doc));
                }
                self.clear_doc_postings(doc, &mut bigram_scratch, false);
            }
            self.write_row_into_doc(doc, row, false, &mut bigram_scratch, &mut tag_ids_scratch);
            if !tags_changed {
                if is_new {
                    if !tag_ids_scratch.is_empty() {
                        tags_changed = true;
                    }
                } else if old_tags_scratch.as_slice() != tag_ids_scratch.as_slice() {
                    tags_changed = true;
                }
            }
        }

        self.title_arena.maybe_compact();
        self.doc_tags.maybe_compact();
        self.maybe_compact_postings();
        self.text_index.maybe_compact();
        tags_changed
    }

    pub fn toggle_special_follow(&mut self, author_id: i64, is_special_follow: bool) {
        if let Some(&h) = self.author_postings.get(&author_id) {
            if is_special_follow {
                h.union_into(&self.postings_slab, &mut self.sf_bitmap);
            } else {
                h.subtract_from(&self.postings_slab, &mut self.sf_bitmap);
            }
        }
    }

    pub fn set_blocked_tags(&mut self, blocked_tags: Vec<String>) {
        self.blocked_tag_names = blocked_tags.into_iter().collect();
        self.recompute_blocked_bitmap();
    }

    #[allow(clippy::too_many_arguments)]
    pub fn apply_delta(
        &mut self,
        delete_ids: &[i64],
        delete_series_ids: &[i64],
        upsert_rows: Vec<NovelRecord>,
        blocked_tags: Option<Vec<String>>,
        exclude_blocked_default: Option<bool>,
        special_follow_changes: &[(i64, bool)],
        aliased_tags: Option<Vec<String>>,
    ) {
        let mut tags_dirty = false;
        if !delete_ids.is_empty() && self.delete_ids(delete_ids) {
            tags_dirty = true;
        }
        if !delete_series_ids.is_empty() && self.delete_series_ids(delete_series_ids) {
            tags_dirty = true;
        }
        if !upsert_rows.is_empty() && self.upsert_batch(upsert_rows) {
            tags_dirty = true;
        }
        if let Some(tags) = blocked_tags {
            self.set_blocked_tags(tags);
        }
        if let Some(val) = exclude_blocked_default {
            self.exclude_blocked_default = val;
        }
        for &(author_id, flag) in special_follow_changes {
            self.toggle_special_follow(author_id, flag);
        }
        if let Some(names) = aliased_tags {
            self.aliased_tag_names = names.into_iter().collect();
            if !self.tag_index.dirty && !tags_dirty {
                self.tag_index
                    .refresh_aliased(&self.tag_interner, &self.aliased_tag_names);
            }
        }
        if tags_dirty {
            self.tag_index.dirty = true;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::NovelIndex;
    use crate::filter::{
        IntFilterField, NumericField, QueryFilter, SortAxis, StrFilterField, TrackCursor,
    };
    use crate::staging::StagingBuilder;

    fn novel(id: i64, tags: Vec<&str>) -> NovelRecord {
        NovelRecord {
            id,
            title: format!("Title {id}"),
            author_id: Some(1),
            author_name: Some("Author".to_string()),
            series_id: None,
            series_name: None,
            series_index: None,
            text: 100,
            like: 10,
            view: 20,
            has_epub: 0,
            create_time_us: Some(id * 1_000_000),
            source: "pixiv".to_string(),
            is_favourite: false,
            is_special_follow: false,
            tags: tags.into_iter().map(str::to_string).collect(),
            chapter_count: 1,
        }
    }

    fn sourced_novel(
        id: i64,
        source: &str,
        author_id: Option<i64>,
        series_id: Option<i64>,
        tags: Vec<&str>,
    ) -> NovelRecord {
        let mut r = novel(id, tags);
        r.source = source.to_string();
        r.author_id = author_id;
        r.author_name = author_id.map(|aid| format!("Author {aid}"));
        r.series_id = series_id;
        r.series_name = series_id.map(|sid| format!("Series {sid}"));
        r.series_index = series_id.map(|_| 1);
        r.like = id.abs() * 10;
        r.create_time_us = Some(id.abs() * 1_000_000);
        r
    }

    fn assert_record_eq(actual: &NovelRecord, expected: &NovelRecord) {
        assert_eq!(actual.id, expected.id);
        assert_eq!(actual.title, expected.title);
        assert_eq!(actual.author_id, expected.author_id);
        assert_eq!(actual.author_name, expected.author_name);
        assert_eq!(actual.series_id, expected.series_id);
        assert_eq!(actual.series_name, expected.series_name);
        assert_eq!(actual.series_index, expected.series_index);
        assert_eq!(actual.text, expected.text);
        assert_eq!(actual.like, expected.like);
        assert_eq!(actual.view, expected.view);
        assert_eq!(actual.has_epub, expected.has_epub);
        assert_eq!(actual.create_time_us, expected.create_time_us);
        assert_eq!(actual.source, expected.source);
        assert_eq!(actual.is_favourite, expected.is_favourite);
        assert_eq!(actual.is_special_follow, expected.is_special_follow);
        assert_eq!(actual.tags, expected.tags);
        assert_eq!(actual.chapter_count, expected.chapter_count);
    }

    fn collect_abs_id_pages(
        engine: &NovelIndex,
        filter: &QueryFilter,
        descending: bool,
        limit: usize,
    ) -> Vec<i64> {
        let mut ids = Vec::new();
        let mut cursor: Option<TrackCursor> = None;
        loop {
            let (rows, has_more, _) = engine
                .query_novel_rows(
                    filter,
                    SortAxis::Numeric(NumericField::AbsId),
                    descending,
                    limit,
                    cursor.as_ref(),
                )
                .unwrap();
            for row in &rows {
                ids.push(row.id);
            }
            if !has_more {
                break;
            }
            let last = rows.last().unwrap();
            cursor = Some(TrackCursor::Int(Some(last.id), last.id));
        }
        ids
    }

    fn assert_query_parity(
        mutated: &NovelIndex,
        expected_rows: &[NovelRecord],
        blocked_tags: &[&str],
        probe_ids: &[i64],
    ) {
        let mut b = StagingBuilder::new(expected_rows.len());
        b.push_batch(expected_rows.to_vec());
        let reference = NovelIndex::new();
        reference.swap_state(b.finalize(
            blocked_tags.iter().map(|s| (*s).to_string()).collect(),
            true,
            vec![],
        ));

        let filters = vec![
            QueryFilter::default(),
            QueryFilter {
                str_filters: vec![(StrFilterField::Source, "esj".into())],
                ..Default::default()
            },
            QueryFilter {
                str_filters: vec![(StrFilterField::Source, "ao3".into())],
                ..Default::default()
            },
            QueryFilter {
                str_filters: vec![(StrFilterField::Source, "pixiv".into())],
                ..Default::default()
            },
            QueryFilter {
                str_filters: vec![(StrFilterField::Source, "local".into())],
                ..Default::default()
            },
            QueryFilter {
                str_filters: vec![(StrFilterField::Source, "esj".into())],
                tags: vec!["isekai".into()],
                ..Default::default()
            },
            QueryFilter {
                str_filters: vec![(StrFilterField::Source, "ao3".into())],
                tags: vec!["fanfic".into()],
                ..Default::default()
            },
            QueryFilter {
                str_filters: vec![(StrFilterField::Source, "ao3".into())],
                keyword_segments: vec!["Rewritten".into()],
                ..Default::default()
            },
            QueryFilter {
                int_filters: vec![(IntFilterField::AuthorId, -100)],
                ..Default::default()
            },
            QueryFilter {
                int_filters: vec![(IntFilterField::AuthorId, -200)],
                ..Default::default()
            },
            QueryFilter {
                int_filters: vec![(IntFilterField::SeriesId, -300)],
                ..Default::default()
            },
            QueryFilter {
                int_filters: vec![(IntFilterField::SeriesId, -400)],
                ..Default::default()
            },
            QueryFilter {
                tags: vec!["old".into()],
                ..Default::default()
            },
            QueryFilter {
                tags: vec!["magic".into()],
                ..Default::default()
            },
            QueryFilter {
                only_special_follow: true,
                ..Default::default()
            },
            QueryFilter {
                only_favourite: true,
                ..Default::default()
            },
            QueryFilter {
                exclude_blocked_tags: Some(false),
                ..Default::default()
            },
            QueryFilter {
                count_blocked: true,
                ..Default::default()
            },
        ];

        for f in &filters {
            assert_eq!(
                mutated.list_matching_ids(f),
                reference.list_matching_ids(f),
                "list_matching_ids parity mismatch for filter {f:?}"
            );
            assert_eq!(
                mutated.count_novels(f, 100),
                reference.count_novels(f, 100),
                "count_novels parity mismatch for filter {f:?}"
            );
            assert_eq!(
                mutated.has_match(f),
                reference.has_match(f),
                "has_match parity mismatch for filter {f:?}"
            );
            assert_eq!(
                collect_abs_id_pages(mutated, f, true, 2),
                collect_abs_id_pages(&reference, f, true, 2),
                "AbsId DESC pagination parity mismatch for filter {f:?}"
            );
            assert_eq!(
                collect_abs_id_pages(mutated, f, false, 2),
                collect_abs_id_pages(&reference, f, false, 2),
                "AbsId ASC pagination parity mismatch for filter {f:?}"
            );
            let mut_like: Vec<i64> = mutated
                .query_novel_rows(f, SortAxis::Numeric(NumericField::Like), true, 20, None)
                .unwrap()
                .0
                .into_iter()
                .map(|r| r.id)
                .collect();
            let ref_like: Vec<i64> = reference
                .query_novel_rows(f, SortAxis::Numeric(NumericField::Like), true, 20, None)
                .unwrap()
                .0
                .into_iter()
                .map(|r| r.id)
                .collect();
            assert_eq!(mut_like, ref_like, "Like sort parity mismatch for {f:?}");
        }

        let got_rows = mutated.get_novel_rows_by_ids(probe_ids);
        let exp_rows = reference.get_novel_rows_by_ids(probe_ids);
        assert_eq!(got_rows.len(), exp_rows.len());
        for (actual, expected) in got_rows.iter().zip(exp_rows.iter()) {
            assert_record_eq(actual, expected);
        }
    }

    #[test]
    fn blocked_tags_match_ascii_case_insensitively() {
        let mut b = StagingBuilder::new(8);
        b.push_batch(vec![
            novel(1, vec!["GAY"]),
            novel(2, vec!["Gay"]),
            novel(3, vec!["Bl"]),
            novel(4, vec!["normal"]),
        ]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec!["gay".into(), "BL".into()], true, vec![]));

        let f = QueryFilter::default();
        assert_eq!(engine.count_novels(&f, 100), (1, false));

        // Upsert with mixed-case blocked tag must also be blocked.
        engine.apply_delta(
            &[],
            &[],
            vec![novel(5, vec!["gAy"]), novel(6, vec!["clean"])],
            None,
            None,
            &[],
            None,
        );
        assert_eq!(engine.count_novels(&f, 100), (2, false));

        // Updating blocked tags with uppercase must match lowercase novel tags.
        engine.apply_delta(
            &[],
            &[],
            vec![novel(7, vec!["ntr"])],
            Some(vec!["NTR".into()]),
            None,
            &[],
            None,
        );
        assert_eq!(engine.count_novels(&f, 100), (6, false));
    }

    #[test]
    fn upsert_and_delete_non_pixiv_sources_and_negative_ids_maintains_postings() {
        let mut b = StagingBuilder::new(4);
        b.push_batch(vec![
            sourced_novel(10, "pixiv", Some(1), None, vec!["pixiv_tag"]),
            sourced_novel(-101, "esj", Some(-10), Some(-20), vec!["old_tag", "shared"]),
        ]);
        let mut state = b.finalize(vec![], true, vec![]);

        // Insert new "ao3" row with negative novel/author/series IDs.
        let r_ao3 = sourced_novel(-102, "ao3", Some(-10), Some(-30), vec!["ao3_tag"]);
        assert!(state.upsert_batch(vec![r_ao3]));
        assert!(state.source_interner.get_id("ao3").is_some());
        assert!(state.local_bitmap.is_empty());
        assert_eq!(
            state.list_matching_ids(&QueryFilter {
                str_filters: vec![(StrFilterField::Source, "ao3".into())],
                ..Default::default()
            }),
            vec![-102]
        );
        assert_eq!(
            state.list_matching_ids(&QueryFilter {
                int_filters: vec![(IntFilterField::AuthorId, -10)],
                ..Default::default()
            }),
            vec![-102, -101]
        );

        // Update -101 changing source/author/series without changing tags -> tags_changed == false.
        let r_reassigned =
            sourced_novel(-101, "ao3", Some(-11), Some(-21), vec!["old_tag", "shared"]);
        assert!(!state.upsert_batch(vec![r_reassigned]));
        assert!(!state.series_postings.contains_key(&-20));
        assert!(state.series_postings.contains_key(&-21));
        assert_eq!(
            state.list_matching_ids(&QueryFilter {
                str_filters: vec![(StrFilterField::Source, "esj".into())],
                ..Default::default()
            }),
            Vec::<i64>::new()
        );
        assert_eq!(
            state.list_matching_ids(&QueryFilter {
                str_filters: vec![(StrFilterField::Source, "ao3".into())],
                ..Default::default()
            }),
            vec![-102, -101]
        );
        assert_eq!(
            state.list_matching_ids(&QueryFilter {
                int_filters: vec![(IntFilterField::AuthorId, -10)],
                ..Default::default()
            }),
            vec![-102]
        );
        assert_eq!(
            state.list_matching_ids(&QueryFilter {
                int_filters: vec![(IntFilterField::AuthorId, -11)],
                ..Default::default()
            }),
            vec![-101]
        );

        // Update tags on -101 -> tags_changed == true and old tag posting is cleared.
        let r_retagged =
            sourced_novel(-101, "ao3", Some(-11), Some(-21), vec!["new_tag", "shared"]);
        assert!(state.upsert_batch(vec![r_retagged]));
        assert!(state
            .list_matching_ids(&QueryFilter {
                tags: vec!["old_tag".into()],
                ..Default::default()
            })
            .is_empty());
        assert_eq!(
            state.list_matching_ids(&QueryFilter {
                tags: vec!["new_tag".into()],
                ..Default::default()
            }),
            vec![-101]
        );

        // Delete -101 (with duplicate & missing negative IDs) -> frees 1 doc slot, prunes postings.
        assert!(state.delete_ids(&[-101, -101, -999_999]));
        assert_eq!(state.free_docs.len(), 1);
        assert!(!state.id_to_doc.contains_key(&-101));
        assert!(!state.author_postings.contains_key(&-11));
        assert!(!state.series_postings.contains_key(&-21));
        assert!(state
            .list_matching_ids(&QueryFilter {
                tags: vec!["new_tag".into()],
                ..Default::default()
            })
            .is_empty());

        // Reusing freed slot for a new "esj" row with NULL author/series must not leak old state.
        let r_reused = sourced_novel(-103, "esj", None, None, vec!["reused_slot"]);
        assert!(state.upsert_batch(vec![r_reused]));
        assert!(state.free_docs.is_empty());
        let doc_103 = *state.id_to_doc.get(&-103).unwrap();
        assert!(state.null_author_id.contains(doc_103));
        assert!(state.null_series_id.contains(doc_103));
        assert_eq!(
            state.list_matching_ids(&QueryFilter {
                str_filters: vec![(StrFilterField::Source, "esj".into())],
                ..Default::default()
            }),
            vec![-103]
        );
        assert_eq!(
            state.list_matching_ids(&QueryFilter {
                str_filters: vec![(StrFilterField::Source, "ao3".into())],
                ..Default::default()
            }),
            vec![-102]
        );
    }

    #[test]
    fn incremental_delta_mutations_match_fresh_rebuild_parity() {
        let mut r_pos10 = sourced_novel(10, "pixiv", Some(100), Some(300), vec!["magic"]);
        r_pos10.is_favourite = true;
        let r_local20 = sourced_novel(20, "local", Some(200), None, vec!["isekai"]);
        let r_neg15 = sourced_novel(-15, "esj", Some(-100), Some(-300), vec!["isekai", "old"]);
        let r_neg25 = sourced_novel(-25, "esj", Some(-100), Some(-300), vec!["isekai"]);
        let r_neg35 = sourced_novel(-35, "ao3", Some(-200), Some(-400), vec!["fanfic"]);

        let mut expected = vec![r_pos10, r_local20, r_neg15, r_neg25, r_neg35];
        let probe_ids = [-65, -55, -45, -35, -25, -15, 10, 20, -999];

        let mut b = StagingBuilder::new(expected.len());
        b.push_batch(expected.clone());
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![]));
        assert_query_parity(&engine, &expected, &[], &probe_ids);

        // Step 1: Insert new "esj" (-45) and "ao3" (-55) rows, and mutate -15 across
        // source ("esj" -> "ao3"), negative author/series, title, tags, and like.
        let r_neg45 = sourced_novel(-45, "esj", Some(-100), Some(-301), vec!["isekai", "magic"]);
        let mut r_neg55 = sourced_novel(-55, "ao3", Some(-201), None, vec!["fanfic"]);
        r_neg55.is_favourite = true;

        let mut r_neg15_mod =
            sourced_novel(-15, "ao3", Some(-200), Some(-400), vec!["fanfic", "magic"]);
        r_neg15_mod.title = "Rewritten ESJ to AO3".to_string();
        r_neg15_mod.series_index = Some(2);
        r_neg15_mod.like = 999;

        engine.apply_delta(
            &[],
            &[],
            vec![r_neg45.clone(), r_neg55.clone(), r_neg15_mod.clone()],
            None,
            None,
            &[],
            None,
        );
        expected.retain(|r| r.id != -15);
        expected.extend([r_neg45, r_neg55, r_neg15_mod]);
        assert_query_parity(&engine, &expected, &[], &probe_ids);

        // Step 2: Delete negative IDs (-35, -45), clear author/series on -25 + apply blocked tag,
        // update blocked tags case-insensitively, and toggle special_follow on negative author -200.
        let mut r_neg25_cleared = sourced_novel(-25, "esj", None, None, vec!["blocked_neg"]);
        r_neg25_cleared.title = "Blocked Neg ESJ".to_string();

        engine.apply_delta(
            &[-35, -45, -35],
            &[],
            vec![r_neg25_cleared.clone()],
            Some(vec!["BLOCKED_NEG".into()]),
            None,
            &[(-200, true)],
            None,
        );
        expected.retain(|r| !matches!(r.id, -35 | -45 | -25));
        expected.push(r_neg25_cleared);
        for row in &mut expected {
            if row.author_id == Some(-200) {
                row.is_special_follow = true;
            }
        }
        assert_query_parity(&engine, &expected, &["BLOCKED_NEG"], &probe_ids);

        // Step 3: Delete -55, insert new "esj" row -65 (reusing freed doc slot),
        // and unset special_follow on negative author -200.
        let mut r_neg65 =
            sourced_novel(-65, "esj", Some(-100), Some(-300), vec!["isekai", "magic"]);
        r_neg65.is_favourite = true;

        engine.apply_delta(
            &[-55],
            &[],
            vec![r_neg65.clone()],
            None,
            None,
            &[(-200, false)],
            None,
        );
        expected.retain(|r| r.id != -55);
        expected.push(r_neg65);
        for row in &mut expected {
            if row.author_id == Some(-200) {
                row.is_special_follow = false;
            }
        }
        assert_query_parity(&engine, &expected, &["BLOCKED_NEG"], &probe_ids);
    }

    #[test]
    fn series_rep_replacement_and_delete_series_ids_clean_up_stale_docs() {
        let mut s1_initial = sourced_novel(-10, "esj", Some(-1), Some(-100), vec!["magic"]);
        s1_initial.chapter_count = 2;
        let mut b = StagingBuilder::new(4);
        b.push_batch(vec![s1_initial]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![]));

        // Replace representative ID (-10 -> -11) for series -100 while incrementing chapter_count.
        let mut s1_new_rep =
            sourced_novel(-11, "esj", Some(-1), Some(-100), vec!["magic", "action"]);
        s1_new_rep.chapter_count = 3;
        engine.apply_delta(&[], &[-100], vec![s1_new_rep], None, None, &[], None);

        let rows = engine
            .query_novel_rows(
                &QueryFilter::default(),
                SortAxis::Numeric(NumericField::AbsId),
                true,
                10,
                None,
            )
            .unwrap()
            .0;
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].id, -11);
        assert_eq!(rows[0].chapter_count, 3);
        assert!(engine.get_novel_rows_by_ids(&[-10]).is_empty());

        // Delete entire series via delete_series_ids.
        engine.apply_delta(&[], &[-100], vec![], None, None, &[], None);
        assert_eq!(
            engine.count_novels(&QueryFilter::default(), 100),
            (0, false)
        );
        assert!(engine.get_novel_rows_by_ids(&[-11]).is_empty());
    }
}
