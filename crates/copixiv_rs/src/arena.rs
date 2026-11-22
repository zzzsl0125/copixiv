use std::mem::size_of;

const COMPACT_MIN_BYTES: usize = 2 * 1024 * 1024;

/// Contiguous slice arena storing variable-length `Copy` sequences indexed by `doc_id`
/// without per-document heap allocations.
pub struct PackedSliceArena<T: Copy> {
    pub data: Vec<T>,
    /// Per `doc_id`: `(offset, len)`. Empty slices have `(0, 0)`.
    pub slots: Vec<(u32, u32)>,
    pub dead_entries: usize,
}

impl<T: Copy> Default for PackedSliceArena<T> {
    fn default() -> Self {
        Self {
            data: Vec::new(),
            slots: Vec::new(),
            dead_entries: 0,
        }
    }
}

impl<T: Copy> PackedSliceArena<T> {
    pub fn with_capacity(docs: usize, approx_entries: usize) -> Self {
        Self {
            data: Vec::with_capacity(approx_entries),
            slots: Vec::with_capacity(docs),
            dead_entries: 0,
        }
    }

    #[inline]
    pub fn get(&self, doc: u32) -> &[T] {
        let Some(&(off, len)) = self.slots.get(doc as usize) else {
            return &[];
        };
        if len == 0 {
            return &[];
        }
        let start = off as usize;
        &self.data[start..start + len as usize]
    }

    pub fn ensure_slot(&mut self, doc: u32) {
        let idx = doc as usize;
        if idx >= self.slots.len() {
            self.slots.resize(idx + 1, (0, 0));
        }
    }

    pub fn set(&mut self, doc: u32, vals: &[T]) {
        self.ensure_slot(doc);
        let idx = doc as usize;
        let (old_off, old_len) = self.slots[idx];
        let new_len = vals.len();

        if new_len == 0 {
            self.dead_entries += old_len as usize;
            self.slots[idx] = (0, 0);
            return;
        }

        if new_len <= old_len as usize {
            let start = old_off as usize;
            self.data[start..start + new_len].copy_from_slice(vals);
            self.dead_entries += old_len as usize - new_len;
            self.slots[idx] = (old_off, new_len as u32);
            return;
        }

        self.dead_entries += old_len as usize;
        let off = self.data.len() as u32;
        self.data.extend_from_slice(vals);
        self.slots[idx] = (off, new_len as u32);
    }

    pub fn clear_slot(&mut self, doc: u32) {
        if let Some(slot) = self.slots.get_mut(doc as usize) {
            self.dead_entries += slot.1 as usize;
            *slot = (0, 0);
        }
    }

    pub fn maybe_compact(&mut self) {
        if self.data.len() * size_of::<T>() <= COMPACT_MIN_BYTES
            || self.dead_entries * 2 <= self.data.len()
        {
            return;
        }
        let live_estimate = self.data.len().saturating_sub(self.dead_entries);
        let mut next_data = Vec::with_capacity(live_estimate);
        for slot in &mut self.slots {
            let (off, len) = *slot;
            if len == 0 {
                *slot = (0, 0);
                continue;
            }
            let new_off = next_data.len() as u32;
            let start = off as usize;
            next_data.extend_from_slice(&self.data[start..start + len as usize]);
            *slot = (new_off, len);
        }
        self.data = next_data;
        self.dead_entries = 0;
    }

    pub fn shrink_to_fit(&mut self) {
        self.data.shrink_to_fit();
        self.slots.shrink_to_fit();
    }
}

impl PackedSliceArena<u8> {
    #[inline]
    pub fn get_bytes(&self, doc: u32) -> &[u8] {
        self.get(doc)
    }

    #[inline]
    pub fn get_str(&self, doc: u32) -> &str {
        // Safety: only valid UTF-8 `&str` bytes are ever pushed via `set_str`.
        unsafe { std::str::from_utf8_unchecked(self.get(doc)) }
    }

    #[inline]
    pub fn set_str(&mut self, doc: u32, text: &str) {
        self.set(doc, text.as_bytes());
    }
}

pub type PackedArena = PackedSliceArena<u8>;
pub type PackedU32Arena = PackedSliceArena<u32>;
