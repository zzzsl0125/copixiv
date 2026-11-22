use rayon::prelude::*;

use crate::filter::{CanonNumKey, HotTrack};
use crate::storage::EngineState;

impl EngineState {
    /// Build a sorted track (`DESC NULLS FIRST, id DESC`) using only `&self`
    /// so readers stay unblocked under `RwLock::upgradable_read`.
    pub fn build_track(&self, hot: HotTrack) -> Vec<u32> {
        let field = hot.field();
        let mut keyed: Vec<(CanonNumKey, u32)> = self
            .alive
            .iter()
            .map(|doc| (self.numeric_key(field, doc), doc))
            .collect();
        keyed.par_sort_unstable_by(|a, b| a.0.cmp(&b.0));
        keyed.into_iter().map(|(_, doc)| doc).collect()
    }

    /// Invalidate dirty hot tracks (preserving `Vec` capacity) without performing
    /// O(N) `Vec::insert/remove` or allocator thrashing inside the write lock.
    pub fn invalidate_hot_tracks(&mut self, mask: u8) {
        for hot in HotTrack::ALL {
            if (mask & hot.bit()) != 0 {
                let idx = hot as usize;
                self.tracks_dirty[idx] = true;
                self.tracks[idx].clear();
            }
        }
    }
}
