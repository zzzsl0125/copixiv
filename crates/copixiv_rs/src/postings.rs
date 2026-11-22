use std::mem::size_of;

use roaring::RoaringBitmap;
use rustc_hash::FxHashMap;
use smallvec::SmallVec;

pub const SMALL_MAX: usize = 64;
pub const MAX_INLINE_2: u32 = 0x7FFF_FFFF;

const TAG_MASK: u64 = 0xF << 60;
const TAG_TWO_BIT: u64 = 0b10 << 62;
const TAG_EMPTY: u64 = 0x0 << 60;
const TAG_ONE: u64 = 0x1 << 60;
const TAG_BITMAP: u64 = 0x2 << 60;
const TAG_SMALL: u64 = 0x3 << 60;

const U32_MASK: u64 = 0xFFFF_FFFF;
const U31_MASK: u64 = 0x7FFF_FFFF;
const OFFSET_MASK: u64 = (1u64 << 40) - 1;
const LEN_SHIFT: u32 = 40;
const CAP_SHIFT: u32 = 48;
const BYTE_MASK: u64 = 0xFF;

const SIZE_CLASSES: [usize; 5] = [4, 8, 16, 32, 64];
const OVERFLOW_SENTINEL: u32 = u32::MAX;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(transparent)]
pub struct PostingHandle(pub u64);

impl Default for PostingHandle {
    #[inline]
    fn default() -> Self {
        Self::EMPTY
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DecodedHandle {
    Empty,
    One(u32),
    Two(u32, u32),
    Small {
        offset: usize,
        len: usize,
        cap: usize,
    },
    Bitmap(u32),
}

impl PostingHandle {
    pub const EMPTY: Self = Self(0);

    #[inline]
    pub const fn is_empty(self) -> bool {
        self.0 == 0
    }

    #[inline]
    pub const fn one(doc: u32) -> Self {
        Self(TAG_ONE | (doc as u64))
    }

    #[inline]
    pub const fn two(a: u32, b: u32) -> Self {
        debug_assert!(a <= MAX_INLINE_2 && b <= MAX_INLINE_2 && a < b);
        Self(TAG_TWO_BIT | ((b as u64) << 31) | (a as u64))
    }

    #[inline]
    pub const fn bitmap(idx: u32) -> Self {
        Self(TAG_BITMAP | (idx as u64))
    }

    #[inline]
    pub const fn small(offset: usize, len: usize, cap: usize) -> Self {
        debug_assert!(len <= SMALL_MAX && cap <= SMALL_MAX && len <= cap);
        debug_assert!(offset as u64 <= OFFSET_MASK);
        Self(
            TAG_SMALL | ((cap as u64) << CAP_SHIFT) | ((len as u64) << LEN_SHIFT) | (offset as u64),
        )
    }

    #[inline]
    pub fn decode(self) -> DecodedHandle {
        if (self.0 & TAG_TWO_BIT) == TAG_TWO_BIT {
            let a = (self.0 & U31_MASK) as u32;
            let b = ((self.0 >> 31) & U31_MASK) as u32;
            return DecodedHandle::Two(a, b);
        }
        match self.0 & TAG_MASK {
            TAG_EMPTY => DecodedHandle::Empty,
            TAG_ONE => DecodedHandle::One((self.0 & U32_MASK) as u32),
            TAG_BITMAP => DecodedHandle::Bitmap((self.0 & U32_MASK) as u32),
            TAG_SMALL => DecodedHandle::Small {
                offset: (self.0 & OFFSET_MASK) as usize,
                len: ((self.0 >> LEN_SHIFT) & BYTE_MASK) as usize,
                cap: ((self.0 >> CAP_SHIFT) & BYTE_MASK) as usize,
            },
            _ => unreachable!("invalid PostingHandle tag"),
        }
    }

    #[inline]
    pub fn len(self, slab: &PostingSlab) -> usize {
        match self.decode() {
            DecodedHandle::Empty => 0,
            DecodedHandle::One(_) => 1,
            DecodedHandle::Two(_, _) => 2,
            DecodedHandle::Small { len, .. } => len,
            DecodedHandle::Bitmap(idx) => slab.bitmap(idx).len() as usize,
        }
    }

    #[cfg(test)]
    #[inline]
    pub fn contains(self, slab: &PostingSlab, doc: u32) -> bool {
        match self.decode() {
            DecodedHandle::Empty => false,
            DecodedHandle::One(a) => a == doc,
            DecodedHandle::Two(a, b) => a == doc || b == doc,
            DecodedHandle::Small { offset, len, .. } => {
                slab.small_slice(offset, len).binary_search(&doc).is_ok()
            }
            DecodedHandle::Bitmap(idx) => slab.bitmap(idx).contains(doc),
        }
    }

    #[inline]
    pub fn iter<'a>(self, slab: &'a PostingSlab) -> PostingIter<'a> {
        match self.decode() {
            DecodedHandle::Empty => PostingIter::Inline {
                buf: [0; 2],
                idx: 0,
                len: 0,
            },
            DecodedHandle::One(a) => PostingIter::Inline {
                buf: [a, 0],
                idx: 0,
                len: 1,
            },
            DecodedHandle::Two(a, b) => PostingIter::Inline {
                buf: [a, b],
                idx: 0,
                len: 2,
            },
            DecodedHandle::Small { offset, len, .. } => {
                PostingIter::Slice(slab.small_slice(offset, len).iter())
            }
            DecodedHandle::Bitmap(idx) => PostingIter::Bitmap(slab.bitmap(idx).iter()),
        }
    }

    pub fn union_into(self, slab: &PostingSlab, out: &mut RoaringBitmap) {
        match self.decode() {
            DecodedHandle::Empty => {}
            DecodedHandle::One(a) => {
                out.insert(a);
            }
            DecodedHandle::Two(a, b) => {
                out.insert(a);
                out.insert(b);
            }
            DecodedHandle::Small { offset, len, .. } => {
                out.extend(slab.small_slice(offset, len).iter().copied());
            }
            DecodedHandle::Bitmap(idx) => {
                *out |= slab.bitmap(idx);
            }
        }
    }

    pub fn subtract_from(self, slab: &PostingSlab, out: &mut RoaringBitmap) {
        match self.decode() {
            DecodedHandle::Empty => {}
            DecodedHandle::One(a) => {
                out.remove(a);
            }
            DecodedHandle::Two(a, b) => {
                out.remove(a);
                out.remove(b);
            }
            DecodedHandle::Small { offset, len, .. } => {
                for &d in slab.small_slice(offset, len) {
                    out.remove(d);
                }
            }
            DecodedHandle::Bitmap(idx) => {
                *out -= slab.bitmap(idx);
            }
        }
    }

    pub fn to_bitmap(self, slab: &PostingSlab) -> RoaringBitmap {
        let mut bm = RoaringBitmap::new();
        self.union_into(slab, &mut bm);
        bm
    }

    pub fn intersect_bitmap(self, slab: &PostingSlab, dst: &mut RoaringBitmap) -> bool {
        match self.decode() {
            DecodedHandle::Empty => {
                dst.clear();
                false
            }
            DecodedHandle::One(a) => {
                let keep = dst.contains(a);
                dst.clear();
                if keep {
                    dst.insert(a);
                }
                keep
            }
            DecodedHandle::Two(a, b) => {
                let ka = dst.contains(a);
                let kb = dst.contains(b);
                dst.clear();
                if ka {
                    dst.insert(a);
                }
                if kb {
                    dst.insert(b);
                }
                ka || kb
            }
            DecodedHandle::Small { offset, len, .. } => {
                let mut kept = [0u32; SMALL_MAX];
                let mut count = 0usize;
                for &d in slab.small_slice(offset, len) {
                    if dst.contains(d) {
                        kept[count] = d;
                        count += 1;
                    }
                }
                dst.clear();
                dst.extend(kept[..count].iter().copied());
                count > 0
            }
            DecodedHandle::Bitmap(idx) => {
                *dst &= slab.bitmap(idx);
                !dst.is_empty()
            }
        }
    }
}

pub enum PostingIter<'a> {
    Inline {
        buf: [u32; 2],
        idx: usize,
        len: usize,
    },
    Slice(std::slice::Iter<'a, u32>),
    Bitmap(roaring::bitmap::Iter<'a>),
}

impl Iterator for PostingIter<'_> {
    type Item = u32;

    #[inline]
    fn next(&mut self) -> Option<Self::Item> {
        match self {
            Self::Inline { buf, idx, len } => {
                if *idx < *len {
                    let v = buf[*idx];
                    *idx += 1;
                    Some(v)
                } else {
                    None
                }
            }
            Self::Slice(it) => it.next().copied(),
            Self::Bitmap(it) => it.next(),
        }
    }
}

#[derive(Default)]
pub struct PostingSlab {
    data: Vec<u32>,
    free_lists: [Vec<u32>; 5],
    bitmaps: Vec<Option<RoaringBitmap>>,
    free_bitmaps: Vec<u32>,
    dead_u32s: usize,
}

impl PostingSlab {
    pub fn new() -> Self {
        Self::default()
    }

    #[inline]
    pub fn small_slice(&self, offset: usize, len: usize) -> &[u32] {
        &self.data[offset..offset + len]
    }

    #[inline]
    pub fn bitmap(&self, idx: u32) -> &RoaringBitmap {
        self.bitmaps[idx as usize]
            .as_ref()
            .expect("dangling PostingHandle::Bitmap")
    }

    #[inline]
    fn size_class(cap: usize) -> Option<usize> {
        match cap {
            4 => Some(0),
            8 => Some(1),
            16 => Some(2),
            32 => Some(3),
            64 => Some(4),
            _ => None,
        }
    }

    #[inline]
    fn cap_for_len(len: usize) -> usize {
        for &c in &SIZE_CLASSES {
            if len <= c {
                return c;
            }
        }
        SMALL_MAX
    }

    fn alloc_small(&mut self, cap: usize) -> usize {
        if let Some(cls) = Self::size_class(cap) {
            if let Some(off) = self.free_lists[cls].pop() {
                return off as usize;
            }
        }
        let off = self.data.len();
        self.data.resize(off + cap, 0);
        off
    }

    fn free_small(&mut self, offset: usize, cap: usize) {
        if let Some(cls) = Self::size_class(cap) {
            self.free_lists[cls].push(offset as u32);
        } else {
            self.dead_u32s += cap;
        }
    }

    fn alloc_bitmap(&mut self, bm: RoaringBitmap) -> u32 {
        if let Some(idx) = self.free_bitmaps.pop() {
            self.bitmaps[idx as usize] = Some(bm);
            idx
        } else {
            let idx = self.bitmaps.len() as u32;
            self.bitmaps.push(Some(bm));
            idx
        }
    }

    fn free_bitmap(&mut self, idx: u32) {
        self.bitmaps[idx as usize] = None;
        self.free_bitmaps.push(idx);
    }

    pub fn insert(&mut self, handle: &mut PostingHandle, doc: u32) -> bool {
        match handle.decode() {
            DecodedHandle::Empty => {
                *handle = PostingHandle::one(doc);
                true
            }
            DecodedHandle::One(a) => {
                if a == doc {
                    return false;
                }
                let (lo, hi) = if a < doc { (a, doc) } else { (doc, a) };
                if hi <= MAX_INLINE_2 {
                    *handle = PostingHandle::two(lo, hi);
                } else {
                    let off = self.alloc_small(4);
                    self.data[off] = lo;
                    self.data[off + 1] = hi;
                    *handle = PostingHandle::small(off, 2, 4);
                }
                true
            }
            DecodedHandle::Two(a, b) => {
                if doc == a || doc == b {
                    return false;
                }
                let arr = if doc < a {
                    [doc, a, b]
                } else if doc < b {
                    [a, doc, b]
                } else {
                    [a, b, doc]
                };
                let off = self.alloc_small(4);
                self.data[off..off + 3].copy_from_slice(&arr);
                *handle = PostingHandle::small(off, 3, 4);
                true
            }
            DecodedHandle::Small { offset, len, cap } => {
                let slice = &self.data[offset..offset + len];
                let pos = if len > 0 && doc > slice[len - 1] {
                    len
                } else {
                    match slice.binary_search(&doc) {
                        Ok(_) => return false,
                        Err(pos) => pos,
                    }
                };
                if len < cap {
                    if pos < len {
                        self.data
                            .copy_within(offset + pos..offset + len, offset + pos + 1);
                    }
                    self.data[offset + pos] = doc;
                    *handle = PostingHandle::small(offset, len + 1, cap);
                    true
                } else if len < SMALL_MAX {
                    let new_cap = Self::cap_for_len(len + 1);
                    let new_off = self.alloc_small(new_cap);
                    self.data.copy_within(offset..offset + pos, new_off);
                    self.data[new_off + pos] = doc;
                    self.data
                        .copy_within(offset + pos..offset + len, new_off + pos + 1);
                    self.free_small(offset, cap);
                    *handle = PostingHandle::small(new_off, len + 1, new_cap);
                    true
                } else {
                    let mut bm = RoaringBitmap::new();
                    let _ = bm.append(self.data[offset..offset + pos].iter().copied());
                    bm.insert(doc);
                    let _ = bm.append(self.data[offset + pos..offset + len].iter().copied());
                    self.free_small(offset, cap);
                    let idx = self.alloc_bitmap(bm);
                    *handle = PostingHandle::bitmap(idx);
                    true
                }
            }
            DecodedHandle::Bitmap(idx) => self.bitmaps[idx as usize]
                .as_mut()
                .expect("dangling PostingHandle::Bitmap")
                .insert(doc),
        }
    }

    pub fn remove(&mut self, handle: &mut PostingHandle, doc: u32) -> bool {
        match handle.decode() {
            DecodedHandle::Empty => false,
            DecodedHandle::One(a) => {
                if a == doc {
                    *handle = PostingHandle::EMPTY;
                    true
                } else {
                    false
                }
            }
            DecodedHandle::Two(a, b) => {
                if doc == a {
                    *handle = PostingHandle::one(b);
                    true
                } else if doc == b {
                    *handle = PostingHandle::one(a);
                    true
                } else {
                    false
                }
            }
            DecodedHandle::Small { offset, len, cap } => {
                let slice = &self.data[offset..offset + len];
                let Ok(pos) = slice.binary_search(&doc) else {
                    return false;
                };
                if pos + 1 < len {
                    self.data
                        .copy_within(offset + pos + 1..offset + len, offset + pos);
                }
                let new_len = len - 1;
                match new_len {
                    0 => {
                        self.free_small(offset, cap);
                        *handle = PostingHandle::EMPTY;
                    }
                    1 => {
                        let a = self.data[offset];
                        self.free_small(offset, cap);
                        *handle = PostingHandle::one(a);
                    }
                    2 if self.data[offset + 1] <= MAX_INLINE_2 => {
                        let a = self.data[offset];
                        let b = self.data[offset + 1];
                        self.free_small(offset, cap);
                        *handle = PostingHandle::two(a, b);
                    }
                    _ => {
                        *handle = PostingHandle::small(offset, new_len, cap);
                    }
                }
                true
            }
            DecodedHandle::Bitmap(idx) => {
                let bm = self.bitmaps[idx as usize]
                    .as_mut()
                    .expect("dangling PostingHandle::Bitmap");
                let removed = bm.remove(doc);
                if removed && bm.is_empty() {
                    self.free_bitmap(idx);
                    *handle = PostingHandle::EMPTY;
                }
                removed
            }
        }
    }

    pub fn maybe_compact<'a>(&mut self, handles: impl IntoIterator<Item = &'a mut PostingHandle>) {
        let dead_free: usize = self
            .free_lists
            .iter()
            .enumerate()
            .map(|(i, fl)| fl.len() * SIZE_CLASSES[i])
            .sum();
        let total_dead = self.dead_u32s + dead_free;
        let small_needs = total_dead >= 65_536 && total_dead * 4 > self.data.len();
        let bitmap_needs =
            self.free_bitmaps.len() >= 256 && self.free_bitmaps.len() * 4 > self.bitmaps.len();
        if small_needs || bitmap_needs {
            self.compact(handles);
        }
    }

    pub fn compact<'a>(&mut self, handles: impl IntoIterator<Item = &'a mut PostingHandle>) {
        let mut new_data: Vec<u32> = Vec::new();
        let mut new_bitmaps: Vec<Option<RoaringBitmap>> = Vec::new();

        for handle in handles {
            match handle.decode() {
                DecodedHandle::Empty | DecodedHandle::One(_) | DecodedHandle::Two(_, _) => {}
                DecodedHandle::Small { offset, len, .. } => match len {
                    0 => *handle = PostingHandle::EMPTY,
                    1 => *handle = PostingHandle::one(self.data[offset]),
                    2 if self.data[offset + 1] <= MAX_INLINE_2 => {
                        *handle = PostingHandle::two(self.data[offset], self.data[offset + 1]);
                    }
                    _ => {
                        let new_off = new_data.len();
                        new_data.extend_from_slice(&self.data[offset..offset + len]);
                        *handle = PostingHandle::small(new_off, len, len);
                    }
                },
                DecodedHandle::Bitmap(idx) => {
                    let bm = self.bitmaps[idx as usize]
                        .take()
                        .expect("dangling PostingHandle::Bitmap");
                    let blen = bm.len() as usize;
                    if blen == 0 {
                        *handle = PostingHandle::EMPTY;
                    } else if blen == 1 {
                        *handle = PostingHandle::one(bm.min().unwrap());
                    } else if blen == 2 {
                        let mut it = bm.iter();
                        let a = it.next().unwrap();
                        let b = it.next().unwrap();
                        if b <= MAX_INLINE_2 {
                            *handle = PostingHandle::two(a, b);
                        } else {
                            let new_off = new_data.len();
                            new_data.push(a);
                            new_data.push(b);
                            *handle = PostingHandle::small(new_off, 2, 2);
                        }
                    } else if blen <= SMALL_MAX {
                        let new_off = new_data.len();
                        new_data.extend(bm.iter());
                        *handle = PostingHandle::small(new_off, blen, blen);
                    } else {
                        let compacted = RoaringBitmap::from_sorted_iter(bm.iter()).unwrap_or(bm);
                        let new_idx = new_bitmaps.len() as u32;
                        new_bitmaps.push(Some(compacted));
                        *handle = PostingHandle::bitmap(new_idx);
                    }
                }
            }
        }

        new_data.shrink_to_fit();
        new_bitmaps.shrink_to_fit();
        self.data = new_data;
        self.bitmaps = new_bitmaps;
        for fl in &mut self.free_lists {
            fl.clear();
            fl.shrink_to_fit();
        }
        self.free_bitmaps.clear();
        self.free_bitmaps.shrink_to_fit();
        self.dead_u32s = 0;
    }

    pub fn approx_heap_bytes(&self) -> usize {
        let data_bytes = self.data.capacity() * size_of::<u32>();
        let free_list_bytes: usize = self
            .free_lists
            .iter()
            .map(|v| v.capacity() * size_of::<u32>())
            .sum();
        let bitmap_vec_bytes = self.bitmaps.capacity() * size_of::<Option<RoaringBitmap>>()
            + self.free_bitmaps.capacity() * size_of::<u32>();
        let bitmap_inner_bytes: usize = self
            .bitmaps
            .iter()
            .filter_map(|opt| opt.as_ref())
            .map(RoaringBitmap::serialized_size)
            .sum();
        data_bytes + free_list_bytes + bitmap_vec_bytes + bitmap_inner_bytes
    }
}

pub enum TitleBucketRef<'a> {
    Single(u32),
    Many(&'a [u32]),
}

impl<'a> TitleBucketRef<'a> {
    #[inline]
    pub fn as_slice(&self) -> &[u32] {
        match self {
            Self::Single(d) => std::slice::from_ref(d),
            Self::Many(s) => s,
        }
    }
}

#[derive(Default)]
pub struct TitleBuckets {
    primary: FxHashMap<u64, u32>,
    overflow: FxHashMap<u64, SmallVec<[u32; 2]>>,
}

impl TitleBuckets {
    pub fn with_capacity(approx: usize) -> Self {
        Self {
            primary: FxHashMap::with_capacity_and_hasher(approx, Default::default()),
            overflow: FxHashMap::default(),
        }
    }

    pub fn insert(&mut self, hash: u64, doc: u32) {
        assert!(
            doc < OVERFLOW_SENTINEL,
            "doc slot exceeded TitleBuckets range"
        );
        use std::collections::hash_map::Entry;
        match self.primary.entry(hash) {
            Entry::Vacant(v) => {
                v.insert(doc);
            }
            Entry::Occupied(mut o) => {
                let cur = *o.get();
                if cur == doc {
                    return;
                }
                if cur == OVERFLOW_SENTINEL {
                    let list = self
                        .overflow
                        .get_mut(&hash)
                        .expect("missing overflow entry");
                    if !list.contains(&doc) {
                        list.push(doc);
                    }
                } else {
                    *o.get_mut() = OVERFLOW_SENTINEL;
                    let mut list = SmallVec::new();
                    list.push(cur);
                    list.push(doc);
                    self.overflow.insert(hash, list);
                }
            }
        }
    }

    pub fn remove(&mut self, hash: u64, doc: u32) {
        let Some(&cur) = self.primary.get(&hash) else {
            return;
        };
        if cur == doc {
            self.primary.remove(&hash);
            return;
        }
        if cur == OVERFLOW_SENTINEL {
            let Some(list) = self.overflow.get_mut(&hash) else {
                return;
            };
            if let Some(pos) = list.iter().position(|&d| d == doc) {
                list.swap_remove(pos);
            }
            if list.len() == 1 {
                let remaining = list[0];
                self.overflow.remove(&hash);
                if let Some(slot) = self.primary.get_mut(&hash) {
                    *slot = remaining;
                }
            } else if list.is_empty() {
                self.overflow.remove(&hash);
                self.primary.remove(&hash);
            }
        }
    }

    #[inline]
    pub fn get(&self, hash: u64) -> Option<TitleBucketRef<'_>> {
        let &cur = self.primary.get(&hash)?;
        if cur == OVERFLOW_SENTINEL {
            self.overflow
                .get(&hash)
                .map(|sv| TitleBucketRef::Many(sv.as_slice()))
        } else {
            Some(TitleBucketRef::Single(cur))
        }
    }

    pub fn shrink_to_fit(&mut self) {
        self.primary.shrink_to_fit();
        self.overflow.shrink_to_fit();
    }

    pub fn approx_heap_bytes(&self) -> usize {
        let primary_bytes = self.primary.capacity() * (size_of::<u64>() + size_of::<u32>() + 4);
        let overflow_bytes = self.overflow.capacity()
            * (size_of::<u64>() + size_of::<SmallVec<[u32; 2]>>())
            + self
                .overflow
                .values()
                .filter(|v| v.spilled())
                .map(|v| v.capacity() * size_of::<u32>())
                .sum::<usize>();
        primary_bytes + overflow_bytes
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handle_size_is_8_bytes() {
        assert_eq!(size_of::<PostingHandle>(), 8);
    }

    #[test]
    fn slab_insert_remove_compact_roundtrip() {
        let mut slab = PostingSlab::new();
        let mut h = PostingHandle::EMPTY;
        assert_eq!(h.len(&slab), 0);

        for doc in 0u32..100 {
            assert!(slab.insert(&mut h, doc * 2));
            assert!(!slab.insert(&mut h, doc * 2));
            assert_eq!(h.len(&slab), (doc + 1) as usize);
        }
        assert!(matches!(h.decode(), DecodedHandle::Bitmap(_)));
        assert!(h.contains(&slab, 198));
        assert!(!h.contains(&slab, 197));

        for doc in (10u32..100).rev() {
            assert!(slab.remove(&mut h, doc * 2));
            assert!(!slab.remove(&mut h, doc * 2));
        }
        assert_eq!(h.len(&slab), 10);

        slab.compact([&mut h]);
        assert!(matches!(
            h.decode(),
            DecodedHandle::Small {
                len: 10,
                cap: 10,
                ..
            }
        ));
        let got: Vec<u32> = h.iter(&slab).collect();
        assert_eq!(got, (0..10).map(|d| d * 2).collect::<Vec<_>>());

        // Growing past exact compacted cap works smoothly
        assert!(slab.insert(&mut h, 999));
        assert_eq!(h.len(&slab), 11);

        for doc in 0u32..10 {
            assert!(slab.remove(&mut h, doc * 2));
        }
        assert!(matches!(h.decode(), DecodedHandle::One(999)));
        assert!(slab.remove(&mut h, 999));
        assert_eq!(h, PostingHandle::EMPTY);
    }

    #[test]
    fn large_inline_2_spills_gracefully() {
        let mut slab = PostingSlab::new();
        let mut h = PostingHandle::EMPTY;
        assert!(slab.insert(&mut h, u32::MAX - 1));
        assert!(matches!(h.decode(), DecodedHandle::One(_)));
        assert!(slab.insert(&mut h, 5));
        assert!(matches!(h.decode(), DecodedHandle::Small { len: 2, .. }));
        slab.compact([&mut h]);
        assert!(matches!(
            h.decode(),
            DecodedHandle::Small { len: 2, cap: 2, .. }
        ));
        assert_eq!(h.iter(&slab).collect::<Vec<_>>(), vec![5, u32::MAX - 1]);
    }

    #[test]
    fn title_buckets_single_and_overflow() {
        let mut buckets = TitleBuckets::default();
        buckets.insert(42, 10);
        assert_eq!(buckets.get(42).unwrap().as_slice(), &[10]);
        buckets.insert(42, 10);
        assert_eq!(buckets.get(42).unwrap().as_slice(), &[10]);

        buckets.insert(42, 20);
        let mut s = buckets.get(42).unwrap().as_slice().to_vec();
        s.sort_unstable();
        assert_eq!(s, vec![10, 20]);

        buckets.remove(42, 10);
        assert_eq!(buckets.get(42).unwrap().as_slice(), &[20]);
        buckets.remove(42, 20);
        assert!(buckets.get(42).is_none());
    }
}
