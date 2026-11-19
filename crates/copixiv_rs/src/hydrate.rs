use crate::interner::StringInterner;
use crate::model::NovelRecord;
use crate::storage::EngineState;

impl EngineState {
    pub fn materialize_doc(&self, doc: u32) -> NovelRecord {
        let idx = doc as usize;
        let id = self.doc_ids[idx];
        let title = self.title_arena.get_str(doc).to_owned();
        let author_id = (!self.null_author_id.contains(doc)).then(|| self.author_id[idx]);
        let an_sid = self.doc_author_name[idx];
        let author_name = (an_sid != StringInterner::NONE)
            .then(|| self.author_name_interner.get_str(an_sid).to_owned());
        let series_id = (!self.null_series_id.contains(doc)).then(|| self.series_id[idx]);
        let sn_sid = self.doc_series_name[idx];
        let series_name = (sn_sid != StringInterner::NONE)
            .then(|| self.series_name_interner.get_str(sn_sid).to_owned());
        let series_index =
            (!self.null_series_index.contains(doc)).then(|| self.series_index[idx] as i64);
        let text = self.texts[idx] as i64;
        let like = self.likes[idx] as i64;
        let view = self.views[idx] as i64;
        let has_epub = self.has_epub[idx] as i32;
        let create_time_us =
            (!self.null_create_time.contains(doc)).then(|| self.create_time_us[idx]);
        let src_sid = self.doc_source[idx];
        let source = if src_sid != StringInterner::NONE {
            self.source_interner.get_str(src_sid).to_owned()
        } else {
            "pixiv".to_string()
        };
        let is_favourite = self.fav_bitmap.contains(doc);
        let is_special_follow = self.sf_bitmap.contains(doc);
        let tags = self
            .doc_tags
            .get(doc)
            .iter()
            .map(|&tid| self.tag_interner.get_str(tid).to_owned())
            .collect();
        NovelRecord {
            id,
            title,
            author_id,
            author_name,
            series_id,
            series_name,
            series_index,
            text,
            like,
            view,
            has_epub,
            create_time_us,
            source,
            is_favourite,
            is_special_follow,
            tags,
        }
    }

    pub fn get_novel_rows_by_ids(&self, novel_ids: &[i64]) -> Vec<NovelRecord> {
        let mut out = Vec::with_capacity(novel_ids.len());
        for &nid in novel_ids {
            if let Some(&doc) = self.id_to_doc.get(&nid) {
                if self.alive.contains(doc) {
                    out.push(self.materialize_doc(doc));
                }
            }
        }
        out
    }
}
