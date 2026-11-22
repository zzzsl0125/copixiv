use rayon::prelude::*;

use crate::filter::HotTrack;
use crate::model::NovelRecord;
use crate::storage::EngineState;

/// Staged snapshot builder used at startup / full reload without holding engine locks.
pub struct StagingBuilder {
    pub state: EngineState,
    bigram_scratch: Vec<u64>,
    tag_ids_scratch: Vec<u32>,
}

impl StagingBuilder {
    pub fn new(capacity: usize) -> Self {
        Self {
            state: EngineState::with_capacity(capacity),
            bigram_scratch: Vec::with_capacity(256),
            tag_ids_scratch: Vec::with_capacity(32),
        }
    }

    pub fn push_batch(&mut self, rows: Vec<NovelRecord>) {
        for row in &rows {
            if self.state.id_to_doc.contains_key(&row.id) {
                continue;
            }
            let doc = self.state.push_empty_doc(row.id);
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
        aliased_tags: Vec<String>,
    ) -> EngineState {
        self.state.blocked_tag_names = blocked_tags.into_iter().collect();
        self.state.exclude_blocked_default = exclude_blocked_default;
        self.state.aliased_tag_names = aliased_tags.into_iter().collect();
        self.state.recompute_blocked_bitmap();

        // Build hot numeric tracks in parallel before atomic swap; `tag_index`
        // derives lazily from `tag_interner` + `tag_postings` on first query.
        let built_tracks: Vec<(HotTrack, Vec<u32>)> = HotTrack::ALL
            .par_iter()
            .map(|&hot| (hot, self.state.build_track(hot)))
            .collect();
        for (hot, track) in built_tracks {
            let idx = hot as usize;
            self.state.tracks[idx] = track;
            self.state.tracks_dirty[idx] = false;
        }
        self.state.optimize_and_shrink();
        self.state.needs_rebuild = false;
        self.state.initialized = true;
        self.state
    }
}
