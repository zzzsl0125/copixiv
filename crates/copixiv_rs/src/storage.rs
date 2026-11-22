use std::mem::size_of;

use roaring::RoaringBitmap;
use rustc_hash::{FxHashMap, FxHashSet};

use crate::arena::{PackedArena, PackedU32Arena};
use crate::filter::{CanonNumKey, HotTrack, NumericField};
use crate::interner::StringInterner;
use crate::postings::{PostingHandle, PostingSlab};
use crate::tag_index::TagIndex;
use crate::text_index::CharBigramIndex;

/// Compact in-memory storage state for all indexed novels and tags.
pub struct EngineState {
    pub initialized: bool,
    pub needs_rebuild: bool,

    pub id_to_doc: FxHashMap<i64, u32>,
    pub doc_ids: Vec<i64>,
    pub alive: RoaringBitmap,
    pub free_docs: Vec<u32>,

    // Narrowed numeric columns (32-bit integers + 8-bit enum match PG column widths).
    pub likes: Vec<i32>,
    pub views: Vec<i32>,
    pub texts: Vec<i32>,
    pub has_epub: Vec<u8>,
    pub create_time_us: Vec<i64>,
    pub author_id: Vec<i64>,
    pub series_id: Vec<i64>,
    pub series_index: Vec<i32>,
    pub null_create_time: RoaringBitmap,
    pub null_author_id: RoaringBitmap,
    pub null_series_id: RoaringBitmap,
    pub null_series_index: RoaringBitmap,

    // Flags & follow state
    pub fav_bitmap: RoaringBitmap,
    pub sf_bitmap: RoaringBitmap,
    pub local_bitmap: RoaringBitmap,

    // Block / default settings
    pub blocked_tag_names: FxHashSet<String>,
    pub blocked_bitmap: RoaringBitmap,
    pub exclude_blocked_default: bool,

    // Inverted postings (8B PostingHandle + contiguous PostingSlab + lasso StringInterners)
    pub postings_slab: PostingSlab,
    pub tag_interner: StringInterner,
    pub tag_postings: Vec<PostingHandle>,
    pub doc_tags: PackedU32Arena,

    pub author_postings: FxHashMap<i64, PostingHandle>,
    pub series_postings: FxHashMap<i64, PostingHandle>,

    pub source_interner: StringInterner,
    pub doc_source: Vec<u32>,

    pub author_name_interner: StringInterner,
    pub author_name_postings: Vec<PostingHandle>,
    pub doc_author_name: Vec<u32>,

    pub series_name_interner: StringInterner,
    pub series_name_postings: Vec<PostingHandle>,
    pub doc_series_name: Vec<u32>,

    // Exact title + title-only sparse char 2-gram index
    pub title_arena: PackedArena,
    pub text_index: CharBigramIndex,

    // Hot pre-sorted tracks (`DESC NULLS FIRST, id DESC`)
    pub tracks: [Vec<u32>; HotTrack::COUNT],
    pub tracks_dirty: [bool; HotTrack::COUNT],

    // In-process tag similarity index (lazily derived from tag_interner + tag_postings)
    pub aliased_tag_names: FxHashSet<String>,
    pub tag_index: TagIndex,
}

impl Default for EngineState {
    fn default() -> Self {
        Self::with_capacity(0)
    }
}

impl EngineState {
    pub fn with_capacity(docs: usize) -> Self {
        Self {
            initialized: false,
            needs_rebuild: false,
            id_to_doc: FxHashMap::with_capacity_and_hasher(docs, Default::default()),
            doc_ids: Vec::with_capacity(docs),
            alive: RoaringBitmap::new(),
            free_docs: Vec::new(),
            likes: Vec::with_capacity(docs),
            views: Vec::with_capacity(docs),
            texts: Vec::with_capacity(docs),
            has_epub: Vec::with_capacity(docs),
            create_time_us: Vec::with_capacity(docs),
            author_id: Vec::with_capacity(docs),
            series_id: Vec::with_capacity(docs),
            series_index: Vec::with_capacity(docs),
            null_create_time: RoaringBitmap::new(),
            null_author_id: RoaringBitmap::new(),
            null_series_id: RoaringBitmap::new(),
            null_series_index: RoaringBitmap::new(),
            fav_bitmap: RoaringBitmap::new(),
            sf_bitmap: RoaringBitmap::new(),
            local_bitmap: RoaringBitmap::new(),
            blocked_tag_names: FxHashSet::default(),
            blocked_bitmap: RoaringBitmap::new(),
            exclude_blocked_default: true,
            postings_slab: PostingSlab::new(),
            tag_interner: StringInterner::new(),
            tag_postings: Vec::new(),
            doc_tags: PackedU32Arena::with_capacity(docs, docs.saturating_mul(12)),
            author_postings: FxHashMap::default(),
            series_postings: FxHashMap::default(),
            source_interner: StringInterner::new(),
            doc_source: Vec::with_capacity(docs),
            author_name_interner: StringInterner::new(),
            author_name_postings: Vec::new(),
            doc_author_name: Vec::with_capacity(docs),
            series_name_interner: StringInterner::new(),
            series_name_postings: Vec::new(),
            doc_series_name: Vec::with_capacity(docs),
            title_arena: PackedArena::with_capacity(docs, docs.saturating_mul(56)),
            text_index: CharBigramIndex::with_capacity(docs.saturating_mul(2)),
            tracks: std::array::from_fn(|_| Vec::new()),
            tracks_dirty: [true; HotTrack::COUNT],
            aliased_tag_names: FxHashSet::default(),
            tag_index: TagIndex::default(),
        }
    }

    #[inline]
    pub fn clamp_i32(val: i64) -> i32 {
        val.clamp(i32::MIN as i64, i32::MAX as i64) as i32
    }

    #[inline]
    pub fn clamp_u8(val: i32) -> u8 {
        val.clamp(0, u8::MAX as i32) as u8
    }

    #[inline]
    pub fn numeric_key(&self, field: NumericField, doc: u32) -> CanonNumKey {
        let idx = doc as usize;
        let nid = self.doc_ids[idx];
        match field {
            NumericField::AbsId => CanonNumKey::from_val(false, nid.unsigned_abs() as i128, nid),
            NumericField::CreateTime => CanonNumKey::from_val(
                self.null_create_time.contains(doc),
                self.create_time_us[idx] as i128,
                nid,
            ),
            NumericField::Like => CanonNumKey::from_val(false, self.likes[idx] as i128, nid),
            NumericField::View => CanonNumKey::from_val(false, self.views[idx] as i128, nid),
            NumericField::Text => CanonNumKey::from_val(false, self.texts[idx] as i128, nid),
        }
    }

    #[inline]
    pub fn field_is_null(&self, field: NumericField, doc: u32) -> bool {
        match field {
            NumericField::CreateTime => self.null_create_time.contains(doc),
            _ => false,
        }
    }

    pub fn compact_postings(&mut self) {
        self.postings_slab.compact(
            self.tag_postings
                .iter_mut()
                .chain(self.author_postings.values_mut())
                .chain(self.series_postings.values_mut())
                .chain(self.author_name_postings.iter_mut())
                .chain(self.series_name_postings.iter_mut()),
        );
    }

    pub fn maybe_compact_postings(&mut self) {
        self.postings_slab.maybe_compact(
            self.tag_postings
                .iter_mut()
                .chain(self.author_postings.values_mut())
                .chain(self.series_postings.values_mut())
                .chain(self.author_name_postings.iter_mut())
                .chain(self.series_name_postings.iter_mut()),
        );
    }

    pub fn optimize_and_shrink(&mut self) {
        self.compact_postings();
        self.text_index.compact_and_shrink();
        self.title_arena.shrink_to_fit();
        self.doc_tags.shrink_to_fit();

        self.id_to_doc.shrink_to_fit();
        self.doc_ids.shrink_to_fit();
        self.likes.shrink_to_fit();
        self.views.shrink_to_fit();
        self.texts.shrink_to_fit();
        self.has_epub.shrink_to_fit();
        self.create_time_us.shrink_to_fit();
        self.author_id.shrink_to_fit();
        self.series_id.shrink_to_fit();
        self.series_index.shrink_to_fit();
        self.doc_source.shrink_to_fit();
        self.doc_author_name.shrink_to_fit();
        self.doc_series_name.shrink_to_fit();
        self.tag_postings.shrink_to_fit();
        self.author_postings.shrink_to_fit();
        self.series_postings.shrink_to_fit();
        self.author_name_postings.shrink_to_fit();
        self.series_name_postings.shrink_to_fit();
        for track in &mut self.tracks {
            track.shrink_to_fit();
        }
    }

    pub fn memory_breakdown(&self) -> Vec<(String, usize, usize)> {
        let id_map_bytes = self.id_to_doc.capacity() * (size_of::<i64>() + size_of::<u32>() + 1);
        let scalar_cols = self.doc_ids.capacity() * 8
            + self.likes.capacity() * 4
            + self.views.capacity() * 4
            + self.texts.capacity() * 4
            + self.has_epub.capacity()
            + self.create_time_us.capacity() * 8
            + self.author_id.capacity() * 8
            + self.series_id.capacity() * 8
            + self.series_index.capacity() * 4
            + self.doc_source.capacity() * 4
            + self.doc_author_name.capacity() * 4
            + self.doc_series_name.capacity() * 4;
        let tracks_bytes: usize = self.tracks.iter().map(|t| t.capacity() * 4).sum();
        let title_arena_bytes =
            self.title_arena.data.capacity() + self.title_arena.slots.capacity() * 8;
        let doc_tags_bytes = self.doc_tags.data.capacity() * 4 + self.doc_tags.slots.capacity() * 8;

        let posting_handles_bytes = self.tag_postings.capacity() * size_of::<PostingHandle>()
            + self.author_postings.capacity() * (size_of::<i64>() + size_of::<PostingHandle>() + 1)
            + self.series_postings.capacity() * (size_of::<i64>() + size_of::<PostingHandle>() + 1)
            + self.author_name_postings.capacity() * size_of::<PostingHandle>()
            + self.series_name_postings.capacity() * size_of::<PostingHandle>();
        let slab_bytes = self.postings_slab.approx_heap_bytes();
        let interner_bytes = self.tag_interner.approx_bytes()
            + self.author_name_interner.approx_bytes()
            + self.series_name_interner.approx_bytes()
            + self.source_interner.approx_bytes();
        let text_bytes = self.text_index.approx_heap_bytes();
        let tag_index_bytes = self.tag_index.approx_bytes();

        let misc_bms = self.alive.serialized_size()
            + self.null_create_time.serialized_size()
            + self.null_author_id.serialized_size()
            + self.null_series_id.serialized_size()
            + self.null_series_index.serialized_size()
            + self.fav_bitmap.serialized_size()
            + self.sf_bitmap.serialized_size()
            + self.local_bitmap.serialized_size()
            + self.blocked_bitmap.serialized_size();

        vec![
            ("id_to_doc".into(), self.id_to_doc.len(), id_map_bytes),
            ("scalar_cols".into(), self.doc_ids.len(), scalar_cols),
            ("hot_tracks".into(), self.tracks.len(), tracks_bytes),
            (
                "title_arena".into(),
                self.title_arena.data.len(),
                title_arena_bytes,
            ),
            ("doc_tags".into(), self.doc_tags.data.len(), doc_tags_bytes),
            ("posting_handles".into(), 0, posting_handles_bytes),
            ("postings_slab".into(), 0, slab_bytes),
            ("interners".into(), 0, interner_bytes),
            (
                "text_index".into(),
                self.text_index.postings.len(),
                text_bytes,
            ),
            (
                "tag_index".into(),
                self.tag_index.tag_count(),
                tag_index_bytes,
            ),
            ("misc_bitmaps".into(), 0, misc_bms),
        ]
    }
}
