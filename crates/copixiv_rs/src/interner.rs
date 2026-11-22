use lasso::{Key, Rodeo, Spur};
use rustc_hash::FxBuildHasher;

/// Single-copy deduplicating `String <-> u32` interner backed by `lasso::Rodeo`.
///
/// Compared with paired `FxHashMap<String, u32> + Vec<String>`, `Rodeo` stores each
/// unique string once in a contiguous string arena with 4-byte `Spur` handles.
pub struct StringInterner {
    rodeo: Rodeo<Spur, FxBuildHasher>,
}

impl Default for StringInterner {
    fn default() -> Self {
        Self::new()
    }
}

impl StringInterner {
    pub const NONE: u32 = u32::MAX;

    pub fn new() -> Self {
        Self {
            rodeo: Rodeo::with_hasher(FxBuildHasher),
        }
    }

    pub fn intern(&mut self, value: &str) -> u32 {
        let spur = self.rodeo.get_or_intern(value);
        let id = spur.into_usize();
        debug_assert!(id < Self::NONE as usize);
        id as u32
    }

    #[inline]
    pub fn get_id(&self, value: &str) -> Option<u32> {
        self.rodeo.get(value).map(|s| s.into_usize() as u32)
    }

    #[inline]
    pub fn get_str(&self, id: u32) -> &str {
        let spur = Spur::try_from_usize(id as usize).expect("valid interner id");
        self.rodeo.resolve(&spur)
    }

    #[inline]
    pub fn len(&self) -> usize {
        self.rodeo.len()
    }

    pub fn approx_bytes(&self) -> usize {
        self.rodeo.strings().map(str::len).sum::<usize>() + self.rodeo.len() * 24
    }

    pub fn matching_ids<F>(&self, mut pred: F) -> Vec<bool>
    where
        F: FnMut(&str) -> bool,
    {
        let n = self.rodeo.len();
        let mut out = Vec::with_capacity(n);
        for i in 0..n {
            let s = self.get_str(i as u32);
            out.push(pred(s));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interner_contiguous_ids_and_roundtrip() {
        let mut interner = StringInterner::new();
        assert_eq!(interner.rodeo.len(), 0);
        let a = interner.intern("fantasy");
        let b = interner.intern("原神");
        let a2 = interner.intern("fantasy");
        assert_eq!(a, 0);
        assert_eq!(b, 1);
        assert_eq!(a2, a);
        assert_eq!(interner.rodeo.len(), 2);
        assert_eq!(interner.get_id("原神"), Some(1));
        assert_eq!(interner.get_id("missing"), None);
        assert_eq!(interner.get_str(0), "fantasy");
        assert_eq!(interner.get_str(1), "原神");
    }
}
