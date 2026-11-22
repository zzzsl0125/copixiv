use std::mem::size_of;

use memchr::memmem::Finder;
use rustc_hash::FxHashMap;

use crate::postings::{PostingHandle, PostingSlab};

/// Pack a pair of ASCII-lowercased characters into a `u64` key.
#[inline]
pub fn pack_char_bigram(c0: char, c1: char) -> u64 {
    ((c0 as u64) << 32) | (c1 as u64)
}

#[inline]
pub fn push_str_bigrams(s: &str, out: &mut Vec<u64>) {
    let mut prev: Option<char> = None;
    for ch in s.chars() {
        let low = ch.to_ascii_lowercase();
        if low.is_whitespace() {
            prev = None;
            continue;
        }
        if let Some(p) = prev {
            out.push(pack_char_bigram(p, low));
        }
        prev = Some(low);
    }
}

/// Collect sorted, deduplicated non-whitespace char 2-grams from a single text field
/// (novel title or tag name). Authors, series, and tags are matched directly via their
/// compact interners instead of bloating the main char-bigram index.
pub fn collect_str_char_bigrams(text: &str, scratch: &mut Vec<u64>) {
    scratch.clear();
    push_str_bigrams(text, scratch);
    scratch.sort_unstable();
    scratch.dedup();
}

#[inline]
pub fn collect_query_char_bigrams(segment_lower: &str, out: &mut Vec<u64>) {
    collect_str_char_bigrams(segment_lower, out);
}

/// Check whether `haystack` contains `finder` under ASCII-only case folding (PG C-locale parity).
/// For non-ASCII needles (CJK, digits, punctuation) no lowercasing or allocation occurs.
#[inline]
pub fn contains_ascii_ci(
    haystack: &[u8],
    needle_len: usize,
    finder: &Finder<'_>,
    need_lower: bool,
) -> bool {
    if haystack.len() < needle_len {
        return false;
    }
    if !need_lower {
        return finder.find(haystack).is_some();
    }
    if haystack.len() <= 512 {
        let mut buf = [0u8; 512];
        let slice = &mut buf[..haystack.len()];
        for (dst, &src) in slice.iter_mut().zip(haystack.iter()) {
            *dst = src.to_ascii_lowercase();
        }
        finder.find(slice).is_some()
    } else {
        let lowered: Vec<u8> = haystack.iter().map(u8::to_ascii_lowercase).collect();
        finder.find(&lowered).is_some()
    }
}

/// Pre-compiled per-segment matcher used during candidate verification.
pub struct SegmentMatcher {
    pub lower: String,
    pub finder: Finder<'static>,
    pub need_lower: bool,
}

impl SegmentMatcher {
    pub fn new(lower: String) -> Self {
        let need_lower = lower.bytes().any(|b| b.is_ascii_lowercase());
        let finder = Finder::new(lower.as_bytes()).into_owned();
        Self {
            lower,
            finder,
            need_lower,
        }
    }

    #[inline]
    pub fn matches_str(&self, haystack: &[u8]) -> bool {
        contains_ascii_ci(haystack, self.lower.len(), &self.finder, self.need_lower)
    }
}

/// Sparse character 2-gram inverted index backed by 8-byte `PostingHandle`s and a contiguous `PostingSlab`.
#[derive(Default)]
pub struct CharBigramIndex {
    pub postings: FxHashMap<u64, PostingHandle>,
    pub slab: PostingSlab,
}

impl CharBigramIndex {
    pub fn with_capacity(approx_bigrams: usize) -> Self {
        Self {
            postings: FxHashMap::with_capacity_and_hasher(approx_bigrams, Default::default()),
            slab: PostingSlab::new(),
        }
    }

    pub fn insert_doc(&mut self, doc: u32, bigrams: &[u64]) {
        for &bg in bigrams {
            let handle = self.postings.entry(bg).or_default();
            self.slab.insert(handle, doc);
        }
    }

    pub fn remove_doc(&mut self, doc: u32, bigrams: &[u64]) {
        for &bg in bigrams {
            if let Some(handle) = self.postings.get_mut(&bg) {
                self.slab.remove(handle, doc);
                if handle.is_empty() {
                    self.postings.remove(&bg);
                }
            }
        }
    }

    #[inline]
    pub fn get(&self, bg: u64) -> Option<PostingHandle> {
        self.postings.get(&bg).copied()
    }

    pub fn compact_and_shrink(&mut self) {
        self.slab.compact(self.postings.values_mut());
        self.postings.shrink_to_fit();
    }

    pub fn maybe_compact(&mut self) {
        self.slab.maybe_compact(self.postings.values_mut());
    }

    pub fn approx_heap_bytes(&self) -> usize {
        let map_bytes =
            self.postings.capacity() * (size_of::<u64>() + size_of::<PostingHandle>() + 1);
        map_bytes + self.slab.approx_heap_bytes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn char_bigram_str_and_ascii_ci() {
        let mut bg = Vec::new();
        collect_str_char_bigrams("Hello Ñ 原神 R-18", &mut bg);
        assert!(bg.contains(&pack_char_bigram('h', 'e')));
        assert!(bg.contains(&pack_char_bigram('r', '-')));
        assert!(bg.contains(&pack_char_bigram('-', '1')));

        let finder = Finder::new(b"hello");
        assert!(contains_ascii_ci(b"Hello World", 5, &finder, true));
        assert!(!contains_ascii_ci(b"World", 5, &finder, true));

        // C-locale parity: 'Ñ' must NOT fold to 'ñ'
        let n_finder = Finder::new("Ñ".as_bytes());
        assert!(!contains_ascii_ci(
            "ñ".as_bytes(),
            "Ñ".len(),
            &n_finder,
            false
        ));
        assert!(contains_ascii_ci(
            "Hello Ñ".as_bytes(),
            "Ñ".len(),
            &n_finder,
            false
        ));
    }
}
