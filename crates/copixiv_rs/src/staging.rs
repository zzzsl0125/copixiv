use rayon::prelude::*;

use crate::filter::HotTrack;
use crate::interner::StringInterner;
use crate::model::NovelRecord;
use crate::storage::EngineState;
use crate::tag_index::TagIndex;

/// Staged snapshot builder used at startup / full reload without holding engine locks.
pub struct StagingBuilder {
    pub state: EngineState,
    bigram_scratch: Vec<u64>,
    tag_ids_scratch: Vec<u32>,
    expected_sequential: u32,
}

impl StagingBuilder {
    pub fn new(capacity: usize) -> Self {
        Self {
            state: EngineState::with_capacity(capacity),
            bigram_scratch: Vec::with_capacity(256),
            tag_ids_scratch: Vec::with_capacity(32),
            expected_sequential: 0,
        }
    }

    pub fn push_batch(&mut self, rows: Vec<NovelRecord>) {
        for row in &rows {
            if self.state.id_to_doc.contains_key(&row.id) {
                continue;
            }
            let doc = self.expected_sequential;
            self.expected_sequential += 1;
            self.state.id_to_doc.insert(row.id, doc);
            self.state.doc_ids.push(row.id);
            self.state.likes.push(0);
            self.state.views.push(0);
            self.state.texts.push(0);
            self.state.has_epub.push(0);
            self.state.create_time_us.push(0);
            self.state.author_id.push(0);
            self.state.series_id.push(0);
            self.state.series_index.push(0);
            self.state.doc_tags.ensure_slot(doc);
            self.state.doc_source.push(StringInterner::NONE);
            self.state.doc_author_name.push(StringInterner::NONE);
            self.state.doc_series_name.push(StringInterner::NONE);
            self.state.write_row_into_doc(
                doc,
                row,
                true,
                &mut self.bigram_scratch,
                &mut self.tag_ids_scratch,
            );
        }
    }

    pub fn finalize(
        mut self,
        blocked_tags: Vec<String>,
        exclude_blocked_default: bool,
        special_follow_authors: Vec<i64>,
        tags: Vec<(i64, String, i32)>,
        aliased_tag_ids: Vec<i64>,
    ) -> EngineState {
        self.state.blocked_tag_names = blocked_tags.into_iter().collect();
        self.state.exclude_blocked_default = exclude_blocked_default;
        for aid in special_follow_authors {
            self.state.special_follow_authors.insert(aid);
            if let Some(&h) = self.state.author_postings.get(&aid) {
                h.union_into(&self.state.postings_slab, &mut self.state.sf_bitmap);
            }
        }
        self.state.recompute_blocked_bitmap();

        // Build hot numeric tracks & 76k tag index in parallel before atomic swap.
        let (built_tracks, built_tag_index) = rayon::join(
            || {
                HotTrack::ALL
                    .par_iter()
                    .map(|&hot| (hot, self.state.build_track(hot)))
                    .collect::<Vec<(HotTrack, Vec<u32>)>>()
            },
            || TagIndex::from_snapshot(tags, aliased_tag_ids),
        );
        for (hot, track) in built_tracks {
            let idx = hot as usize;
            self.state.tracks[idx] = track;
            self.state.tracks_dirty[idx] = false;
        }
        self.state.tag_index = built_tag_index;
        self.state.optimize_and_shrink();
        self.state.needs_rebuild = false;
        self.state.initialized = true;
        self.state
    }
}
