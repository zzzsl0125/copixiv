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

        let mut tags_changed = false;
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

    pub fn apply_delta(
        &mut self,
        delete_ids: &[i64],
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
    use crate::filter::QueryFilter;
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
            vec![novel(7, vec!["ntr"])],
            Some(vec!["NTR".into()]),
            None,
            &[],
            None,
        );
        assert_eq!(engine.count_novels(&f, 100), (6, false));
    }
}
