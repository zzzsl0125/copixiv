use roaring::RoaringBitmap;

use crate::postings::{PostingHandle, PostingSlab};

pub enum CandidateMask {
    AllAlive,
    Bitmap(RoaringBitmap),
}

impl CandidateMask {
    #[inline]
    pub fn as_bitmap<'a>(&'a self, alive: &'a RoaringBitmap) -> &'a RoaringBitmap {
        match self {
            CandidateMask::AllAlive => alive,
            CandidateMask::Bitmap(bm) => bm,
        }
    }

    pub fn intersect_bm(&mut self, other: &RoaringBitmap) {
        match self {
            CandidateMask::AllAlive => *self = CandidateMask::Bitmap(other.clone()),
            CandidateMask::Bitmap(cur) => *cur &= other,
        }
    }

    pub fn intersect_posting(&mut self, handle: PostingHandle, slab: &PostingSlab) {
        match self {
            CandidateMask::AllAlive => *self = CandidateMask::Bitmap(handle.to_bitmap(slab)),
            CandidateMask::Bitmap(cur) => {
                handle.intersect_bitmap(slab, cur);
            }
        }
    }

    pub fn subtract_bm(&mut self, other: &RoaringBitmap, alive: &RoaringBitmap) {
        if other.is_empty() {
            return;
        }
        match self {
            CandidateMask::AllAlive => *self = CandidateMask::Bitmap(alive - other),
            CandidateMask::Bitmap(cur) => *cur -= other,
        }
    }

    #[inline]
    pub fn contains(&self, doc: u32) -> bool {
        match self {
            CandidateMask::AllAlive => true,
            CandidateMask::Bitmap(bm) => bm.contains(doc),
        }
    }

    #[inline]
    pub fn contains_slot(&self, doc: u32, alive: &RoaringBitmap) -> bool {
        self.as_bitmap(alive).contains(doc)
    }

    #[inline]
    pub fn is_empty(&self, alive: &RoaringBitmap) -> bool {
        self.as_bitmap(alive).is_empty()
    }

    #[inline]
    pub fn len(&self, alive: &RoaringBitmap) -> u64 {
        self.as_bitmap(alive).len()
    }
}
