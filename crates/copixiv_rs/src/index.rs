use std::cmp::{Ordering, Reverse};

use memchr::memmem::Finder;
use parking_lot::RwLock;
use rayon::prelude::*;
use roaring::RoaringBitmap;
use rustc_hash::{FxHashMap, FxHashSet, FxHasher};
use std::hash::{Hash, Hasher};

use crate::arena::{PackedArena, StringInterner};
use crate::filter::{
    IntFilterField, NumericField, QueryFilter, SortAxis, StrFilterField, StringSortField,
    TrackCursor,
};
use crate::random::{
    collect_top_k, fresh_seed, hex16_encode, top_k_random_bitmap, top_k_random_parallel,
};

/// Input row passed from PostgreSQL (slim columns, without heavy `content`).
#[derive(Clone, Debug)]
pub struct NovelRow {
    pub id: i64,
    pub title: String,
    pub author_id: Option<i64>,
    pub author_name: Option<String>,
    pub series_id: Option<i64>,
    pub series_name: Option<String>,
    pub series_index: Option<i64>,
    pub text: i64,
    pub like: i64,
    pub view: i64,
    pub has_epub: i32,
    pub create_time_us: Option<i64>,
    pub source: String,
    pub is_favourite: bool,
    pub is_special_follow: bool,
    pub tags: Vec<String>,
}

/// Canonical numeric sort key in `DESC NULLS FIRST, id DESC` order:
/// smaller `(Reverse(is_null), Reverse(val_i128), Reverse(id))` sorts EARLIER.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct CanonNumKey {
    is_null_rev: Reverse<bool>,
    val_rev: Reverse<i128>,
    id_rev: Reverse<i64>,
}

impl CanonNumKey {
    #[inline]
    fn from_val(is_null: bool, val: i128, novel_id: i64) -> Self {
        Self {
            is_null_rev: Reverse(is_null),
            val_rev: Reverse(if is_null { 0 } else { val }),
            id_rev: Reverse(novel_id),
        }
    }
}

/// Output rank wrapper for cold string sorts (`binary_heap` keeps `limit+1` smallest items).
#[derive(Eq, PartialEq)]
struct StringRankItem<'a> {
    descending: bool,
    is_null: bool,
    val: &'a str,
    novel_id: i64,
    doc: u32,
}

impl Ord for StringRankItem<'_> {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.descending {
            // DESC NULLS FIRST, id DESC -> smaller (earlier) means NULL first, larger val, larger id
            other
                .is_null
                .cmp(&self.is_null)
                .then_with(|| {
                    if self.is_null {
                        Ordering::Equal
                    } else {
                        other.val.cmp(self.val)
                    }
                })
                .then_with(|| other.novel_id.cmp(&self.novel_id))
        } else {
            // ASC NULLS LAST, id ASC -> smaller (earlier) means non-NULL first, smaller val, smaller id
            self.is_null
                .cmp(&other.is_null)
                .then_with(|| {
                    if self.is_null {
                        Ordering::Equal
                    } else {
                        self.val.cmp(other.val)
                    }
                })
                .then_with(|| self.novel_id.cmp(&other.novel_id))
        }
    }
}

impl PartialOrd for StringRankItem<'_> {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

#[derive(Eq, PartialEq)]
struct NumericRankItem {
    descending: bool,
    key: CanonNumKey,
    novel_id: i64,
    doc: u32,
}

impl Ord for NumericRankItem {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.descending {
            self.key.cmp(&other.key)
        } else {
            other.key.cmp(&self.key)
        }
    }
}

impl PartialOrd for NumericRankItem {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

#[inline]
fn hash_bytes(bytes: &[u8]) -> u64 {
    let mut h = FxHasher::default();
    bytes.hash(&mut h);
    h.finish()
}

pub fn build_search_lower(
    title: &str,
    author_name: Option<&str>,
    series_name: Option<&str>,
    tags: &[String],
) -> String {
    let tags_joined = if tags.is_empty() {
        None
    } else {
        let joined = tags.join(" ");
        if joined.is_empty() {
            None
        } else {
            Some(joined)
        }
    };

    let mut out = String::with_capacity(
        title.len()
            + author_name.map_or(0, str::len)
            + series_name.map_or(0, str::len)
            + tags_joined.as_ref().map_or(0, String::len)
            + 8,
    );
    let parts = [
        (!title.is_empty()).then_some(title),
        author_name.filter(|s| !s.is_empty()),
        series_name.filter(|s| !s.is_empty()),
        tags_joined.as_deref(),
    ];
    for part in parts.into_iter().flatten() {
        if !out.is_empty() {
            out.push(' ');
        }
        out.push_str(part);
    }
    out.make_ascii_lowercase();
    out
}

#[inline]
fn collect_bigrams_into(bytes: &[u8], scratch: &mut Vec<u16>) {
    scratch.clear();
    if bytes.len() < 2 {
        return;
    }
    scratch.reserve(bytes.len() - 1);
    for w in bytes.windows(2) {
        scratch.push(((w[0] as u16) << 8) | (w[1] as u16));
    }
    scratch.sort_unstable();
    scratch.dedup();
}

pub struct EngineState {
    pub initialized: bool,
    pub id_to_doc: FxHashMap<i64, u32>,
    pub doc_ids: Vec<i64>,
    pub alive: RoaringBitmap,
    pub free_docs: Vec<u32>,

    // Numeric columns
    pub likes: Vec<i64>,
    pub views: Vec<i64>,
    pub texts: Vec<i64>,
    pub has_epub: Vec<i32>,
    pub create_time_us: Vec<i64>,
    pub author_id: Vec<i64>,
    pub series_id: Vec<i64>,
    pub series_index: Vec<i64>,
    pub null_create_time: RoaringBitmap,
    pub null_author_id: RoaringBitmap,
    pub null_series_id: RoaringBitmap,
    pub null_series_index: RoaringBitmap,

    // Flags & follow state
    pub fav_bitmap: RoaringBitmap,
    pub sf_bitmap: RoaringBitmap,
    pub special_follow_authors: FxHashSet<i64>,

    // Block / default settings
    pub blocked_tag_names: FxHashSet<String>,
    pub blocked_bitmap: RoaringBitmap,
    pub exclude_blocked_default: bool,

    // Inverted postings
    pub tag_interner: StringInterner,
    pub tag_bitmaps: Vec<RoaringBitmap>,
    pub doc_tags: Vec<Vec<u32>>,

    pub author_bitmaps: FxHashMap<i64, RoaringBitmap>,
    pub series_bitmaps: FxHashMap<i64, RoaringBitmap>,
    pub epub_bitmaps: FxHashMap<i32, RoaringBitmap>,

    pub source_interner: StringInterner,
    pub source_bitmaps: Vec<RoaringBitmap>,
    pub doc_source: Vec<u32>,

    pub author_name_interner: StringInterner,
    pub author_name_bitmaps: Vec<RoaringBitmap>,
    pub doc_author_name: Vec<u32>,

    pub series_name_interner: StringInterner,
    pub series_name_bitmaps: Vec<RoaringBitmap>,
    pub doc_series_name: Vec<u32>,

    // Exact title + lower search text
    pub title_arena: PackedArena,
    pub title_hash_buckets: FxHashMap<u64, RoaringBitmap>,
    pub search_arena: PackedArena,
    pub bigrams: Vec<RoaringBitmap>, // len == 65536

    // Pre-sorted tracks (DESC NULLS FIRST, id DESC)
    pub tracks: [Vec<u32>; NumericField::COUNT],
    pub tracks_dirty: [bool; NumericField::COUNT],
}

impl Default for EngineState {
    fn default() -> Self {
        Self::with_capacity(0)
    }
}

enum CandidateMask {
    AllAlive,
    Bitmap(RoaringBitmap),
}

impl CandidateMask {
    fn intersect_bm(&mut self, other: &RoaringBitmap) {
        match self {
            CandidateMask::AllAlive => *self = CandidateMask::Bitmap(other.clone()),
            CandidateMask::Bitmap(cur) => *cur &= other,
        }
    }

    fn subtract_bm(&mut self, other: &RoaringBitmap, alive: &RoaringBitmap) {
        if other.is_empty() {
            return;
        }
        match self {
            CandidateMask::AllAlive => *self = CandidateMask::Bitmap(alive - other),
            CandidateMask::Bitmap(cur) => *cur -= other,
        }
    }

    #[inline]
    fn contains(&self, doc: u32) -> bool {
        match self {
            CandidateMask::AllAlive => true,
            CandidateMask::Bitmap(bm) => bm.contains(doc),
        }
    }

    #[inline]
    fn contains_slot(&self, doc: u32, alive: &RoaringBitmap) -> bool {
        match self {
            CandidateMask::AllAlive => alive.contains(doc),
            CandidateMask::Bitmap(bm) => bm.contains(doc),
        }
    }

    #[inline]
    fn is_empty(&self, alive: &RoaringBitmap) -> bool {
        match self {
            CandidateMask::AllAlive => alive.is_empty(),
            CandidateMask::Bitmap(bm) => bm.is_empty(),
        }
    }

    #[inline]
    fn len(&self, alive: &RoaringBitmap) -> u64 {
        match self {
            CandidateMask::AllAlive => alive.len(),
            CandidateMask::Bitmap(bm) => bm.len(),
        }
    }
}

impl EngineState {
    pub fn with_capacity(docs: usize) -> Self {
        Self {
            initialized: false,
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
            special_follow_authors: FxHashSet::default(),
            blocked_tag_names: FxHashSet::default(),
            blocked_bitmap: RoaringBitmap::new(),
            exclude_blocked_default: true,
            tag_interner: StringInterner::default(),
            tag_bitmaps: Vec::new(),
            doc_tags: Vec::with_capacity(docs),
            author_bitmaps: FxHashMap::default(),
            series_bitmaps: FxHashMap::default(),
            epub_bitmaps: FxHashMap::default(),
            source_interner: StringInterner::default(),
            source_bitmaps: Vec::new(),
            doc_source: Vec::with_capacity(docs),
            author_name_interner: StringInterner::default(),
            author_name_bitmaps: Vec::new(),
            doc_author_name: Vec::with_capacity(docs),
            series_name_interner: StringInterner::default(),
            series_name_bitmaps: Vec::new(),
            doc_series_name: Vec::with_capacity(docs),
            title_arena: PackedArena::with_capacity(docs, docs * 48),
            title_hash_buckets: FxHashMap::default(),
            search_arena: PackedArena::with_capacity(docs, docs * 160),
            bigrams: (0..65_536).map(|_| RoaringBitmap::new()).collect(),
            tracks: std::array::from_fn(|_| Vec::new()),
            tracks_dirty: [true; NumericField::COUNT],
        }
    }

    #[inline]
    fn numeric_key(&self, field: NumericField, doc: u32) -> CanonNumKey {
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
            NumericField::AuthorId => CanonNumKey::from_val(
                self.null_author_id.contains(doc),
                self.author_id[idx] as i128,
                nid,
            ),
            NumericField::SeriesId => CanonNumKey::from_val(
                self.null_series_id.contains(doc),
                self.series_id[idx] as i128,
                nid,
            ),
            NumericField::SeriesIndex => CanonNumKey::from_val(
                self.null_series_index.contains(doc),
                self.series_index[idx] as i128,
                nid,
            ),
            NumericField::HasEpub => {
                CanonNumKey::from_val(false, self.has_epub[idx] as i128, nid)
            }
        }
    }

    #[inline]
    fn field_is_null(&self, field: NumericField, doc: u32) -> bool {
        match field {
            NumericField::CreateTime => self.null_create_time.contains(doc),
            NumericField::AuthorId => self.null_author_id.contains(doc),
            NumericField::SeriesId => self.null_series_id.contains(doc),
            NumericField::SeriesIndex => self.null_series_index.contains(doc),
            _ => false,
        }
    }

    #[inline]
    fn numeric_val_i128(&self, field: NumericField, doc: u32) -> i128 {
        let idx = doc as usize;
        match field {
            NumericField::AbsId => self.doc_ids[idx].unsigned_abs() as i128,
            NumericField::CreateTime => self.create_time_us[idx] as i128,
            NumericField::Like => self.likes[idx] as i128,
            NumericField::View => self.views[idx] as i128,
            NumericField::Text => self.texts[idx] as i128,
            NumericField::AuthorId => self.author_id[idx] as i128,
            NumericField::SeriesId => self.series_id[idx] as i128,
            NumericField::SeriesIndex => self.series_index[idx] as i128,
            NumericField::HasEpub => self.has_epub[idx] as i128,
        }
    }

    #[inline]
    fn string_val(&self, field: StringSortField, doc: u32) -> (bool, &str) {
        let idx = doc as usize;
        match field {
            StringSortField::Title => (false, self.title_arena.get_str(doc)),
            StringSortField::AuthorName => {
                let id = self.doc_author_name[idx];
                if id == StringInterner::NONE {
                    (true, "")
                } else {
                    (false, self.author_name_interner.get_str(id))
                }
            }
            StringSortField::SeriesName => {
                let id = self.doc_series_name[idx];
                if id == StringInterner::NONE {
                    (true, "")
                } else {
                    (false, self.series_name_interner.get_str(id))
                }
            }
        }
    }

    pub fn rebuild_track(&mut self, field: NumericField) {
        let f_idx = field as usize;
        let mut keyed: Vec<(CanonNumKey, u32)> = self
            .alive
            .iter()
            .map(|doc| (self.numeric_key(field, doc), doc))
            .collect();
        keyed.par_sort_unstable_by(|a, b| a.0.cmp(&b.0));
        self.tracks[f_idx] = keyed.into_iter().map(|(_, doc)| doc).collect();
        self.tracks_dirty[f_idx] = false;
    }

    fn alloc_doc(&mut self, novel_id: i64) -> (u32, bool) {
        if let Some(&doc) = self.id_to_doc.get(&novel_id) {
            return (doc, false);
        }
        if let Some(doc) = self.free_docs.pop() {
            self.id_to_doc.insert(novel_id, doc);
            self.doc_ids[doc as usize] = novel_id;
            return (doc, true);
        }
        let doc = self.doc_ids.len() as u32;
        self.id_to_doc.insert(novel_id, doc);
        self.doc_ids.push(novel_id);
        self.likes.push(0);
        self.views.push(0);
        self.texts.push(0);
        self.has_epub.push(0);
        self.create_time_us.push(0);
        self.author_id.push(0);
        self.series_id.push(0);
        self.series_index.push(0);
        self.doc_tags.push(Vec::new());
        self.doc_source.push(StringInterner::NONE);
        self.doc_author_name.push(StringInterner::NONE);
        self.doc_series_name.push(StringInterner::NONE);
        (doc, true)
    }

    fn clear_doc_postings(
        &mut self,
        doc: u32,
        bigram_scratch: &mut Vec<u16>,
        for_delete: bool,
    ) {
        let idx = doc as usize;
        self.fav_bitmap.remove(doc);
        self.sf_bitmap.remove(doc);
        self.blocked_bitmap.remove(doc);

        self.null_create_time.remove(doc);
        if !self.null_author_id.remove(doc) {
            let aid = self.author_id[idx];
            if let Some(bm) = self.author_bitmaps.get_mut(&aid) {
                bm.remove(doc);
                if bm.is_empty() {
                    self.author_bitmaps.remove(&aid);
                }
            }
        }
        if !self.null_series_id.remove(doc) {
            let sid = self.series_id[idx];
            if let Some(bm) = self.series_bitmaps.get_mut(&sid) {
                bm.remove(doc);
                if bm.is_empty() {
                    self.series_bitmaps.remove(&sid);
                }
            }
        }
        self.null_series_index.remove(doc);

        let he = self.has_epub[idx];
        if let Some(bm) = self.epub_bitmaps.get_mut(&he) {
            bm.remove(doc);
            if bm.is_empty() {
                self.epub_bitmaps.remove(&he);
            }
        }

        let src_id = self.doc_source[idx];
        if src_id != StringInterner::NONE {
            self.source_bitmaps[src_id as usize].remove(doc);
            self.doc_source[idx] = StringInterner::NONE;
        }
        let an_id = self.doc_author_name[idx];
        if an_id != StringInterner::NONE {
            self.author_name_bitmaps[an_id as usize].remove(doc);
            self.doc_author_name[idx] = StringInterner::NONE;
        }
        let sn_id = self.doc_series_name[idx];
        if sn_id != StringInterner::NONE {
            self.series_name_bitmaps[sn_id as usize].remove(doc);
            self.doc_series_name[idx] = StringInterner::NONE;
        }

        for tid in self.doc_tags[idx].drain(..) {
            self.tag_bitmaps[tid as usize].remove(doc);
        }

        let old_title = self.title_arena.get_bytes(doc);
        if self.alive.contains(doc) {
            let th = hash_bytes(old_title);
            if let Some(bm) = self.title_hash_buckets.get_mut(&th) {
                bm.remove(doc);
                if bm.is_empty() {
                    self.title_hash_buckets.remove(&th);
                }
            }
        }

        let old_search = self.search_arena.get_bytes(doc);
        if !old_search.is_empty() {
            collect_bigrams_into(old_search, bigram_scratch);
            for &bg in bigram_scratch.iter() {
                self.bigrams[bg as usize].remove(doc);
            }
        }

        if for_delete {
            self.title_arena.clear_slot(doc);
            self.search_arena.clear_slot(doc);
        }
    }

    fn write_row_into_doc(
        &mut self,
        doc: u32,
        row: &NovelRow,
        sequential_push: bool,
        bigram_scratch: &mut Vec<u16>,
    ) {
        let idx = doc as usize;
        if sequential_push {
            self.alive.push(doc);
        } else {
            self.alive.insert(doc);
        }

        self.likes[idx] = row.like;
        self.views[idx] = row.view;
        self.texts[idx] = row.text;
        self.has_epub[idx] = row.has_epub;

        match row.create_time_us {
            Some(us) => self.create_time_us[idx] = us,
            None => {
                self.create_time_us[idx] = 0;
                insert_bm(&mut self.null_create_time, doc, sequential_push);
            }
        }

        match row.author_id {
            Some(aid) => {
                self.author_id[idx] = aid;
                let bm = self.author_bitmaps.entry(aid).or_default();
                insert_bm(bm, doc, sequential_push);
            }
            None => {
                self.author_id[idx] = 0;
                insert_bm(&mut self.null_author_id, doc, sequential_push);
            }
        }

        match row.series_id {
            Some(sid) => {
                self.series_id[idx] = sid;
                let bm = self.series_bitmaps.entry(sid).or_default();
                insert_bm(bm, doc, sequential_push);
            }
            None => {
                self.series_id[idx] = 0;
                insert_bm(&mut self.null_series_id, doc, sequential_push);
            }
        }

        match row.series_index {
            Some(sidx) => self.series_index[idx] = sidx,
            None => {
                self.series_index[idx] = 0;
                insert_bm(&mut self.null_series_index, doc, sequential_push);
            }
        }

        insert_bm(self.epub_bitmaps.entry(row.has_epub).or_default(), doc, sequential_push);

        if row.is_favourite {
            insert_bm(&mut self.fav_bitmap, doc, sequential_push);
        }

        let sf = row.is_special_follow
            || row
                .author_id
                .is_some_and(|aid| self.special_follow_authors.contains(&aid));
        if sf {
            insert_bm(&mut self.sf_bitmap, doc, sequential_push);
            if let Some(aid) = row.author_id {
                if row.is_special_follow {
                    self.special_follow_authors.insert(aid);
                }
            }
        }

        let src_id = intern_posting(
            &mut self.source_interner,
            &mut self.source_bitmaps,
            &row.source,
            doc,
            sequential_push,
        );
        self.doc_source[idx] = src_id;

        if let Some(ref an) = row.author_name {
            let an_id = intern_posting(
                &mut self.author_name_interner,
                &mut self.author_name_bitmaps,
                an,
                doc,
                sequential_push,
            );
            self.doc_author_name[idx] = an_id;
        } else {
            self.doc_author_name[idx] = StringInterner::NONE;
        }

        if let Some(ref sn) = row.series_name {
            let sn_id = intern_posting(
                &mut self.series_name_interner,
                &mut self.series_name_bitmaps,
                sn,
                doc,
                sequential_push,
            );
            self.doc_series_name[idx] = sn_id;
        } else {
            self.doc_series_name[idx] = StringInterner::NONE;
        }

        let mut is_blocked = false;
        let doc_tag_ids = &mut self.doc_tags[idx];
        doc_tag_ids.reserve(row.tags.len());
        for tag in &row.tags {
            let tid = intern_posting(
                &mut self.tag_interner,
                &mut self.tag_bitmaps,
                tag,
                doc,
                sequential_push,
            );
            if !doc_tag_ids.contains(&tid) {
                doc_tag_ids.push(tid);
            }
            if !sequential_push && self.blocked_tag_names.contains(tag) {
                is_blocked = true;
            }
        }
        if is_blocked {
            self.blocked_bitmap.insert(doc);
        }

        self.title_arena.set(doc, &row.title);
        let th = hash_bytes(row.title.as_bytes());
        insert_bm(
            self.title_hash_buckets.entry(th).or_default(),
            doc,
            sequential_push,
        );

        let search_lower = build_search_lower(
            &row.title,
            row.author_name.as_deref(),
            row.series_name.as_deref(),
            &row.tags,
        );
        collect_bigrams_into(search_lower.as_bytes(), bigram_scratch);
        for &bg in bigram_scratch.iter() {
            insert_bm(&mut self.bigrams[bg as usize], doc, sequential_push);
        }
        self.search_arena.set(doc, &search_lower);
    }

    pub fn recompute_blocked_bitmap(&mut self) {
        self.blocked_bitmap.clear();
        for name in &self.blocked_tag_names {
            if let Some(tid) = self.tag_interner.get_id(name) {
                self.blocked_bitmap |= &self.tag_bitmaps[tid as usize];
            }
        }
    }

    fn remove_doc_from_track(&mut self, field: NumericField, doc: u32) {
        let f_idx = field as usize;
        if self.tracks_dirty[f_idx] {
            return;
        }
        let key = self.numeric_key(field, doc);
        let track = &self.tracks[f_idx];
        if let Ok(pos) = track.binary_search_by(|&probe| self.numeric_key(field, probe).cmp(&key)) {
            self.tracks[f_idx].remove(pos);
        }
    }

    fn insert_doc_into_track(&mut self, field: NumericField, doc: u32) {
        let f_idx = field as usize;
        if self.tracks_dirty[f_idx] {
            return;
        }
        let key = self.numeric_key(field, doc);
        let pos = self.tracks[f_idx]
            .partition_point(|&probe| self.numeric_key(field, probe) < key);
        self.tracks[f_idx].insert(pos, doc);
    }

    fn invalidate_tracks(&mut self, mask: u16) {
        for field in NumericField::ALL {
            if (mask & field.bit()) != 0 {
                let idx = field as usize;
                self.tracks_dirty[idx] = true;
                self.tracks[idx].clear();
            }
        }
    }

    pub fn delete_ids(&mut self, novel_ids: &[i64]) {
        let docs: Vec<u32> = novel_ids
            .iter()
            .filter_map(|nid| self.id_to_doc.get(nid).copied())
            .filter(|&doc| self.alive.contains(doc))
            .collect();
        if docs.is_empty() {
            return;
        }

        if docs.len() <= 16 {
            for &doc in &docs {
                for field in NumericField::ALL {
                    if !self.tracks_dirty[field as usize] {
                        self.remove_doc_from_track(field, doc);
                    }
                }
            }
        } else {
            self.invalidate_tracks(u16::MAX);
        }

        let mut bigram_scratch = Vec::with_capacity(256);
        for doc in docs {
            let nid = self.doc_ids[doc as usize];
            self.clear_doc_postings(doc, &mut bigram_scratch, true);
            self.alive.remove(doc);
            self.id_to_doc.remove(&nid);
            self.free_docs.push(doc);
        }
        self.title_arena.maybe_compact();
        self.search_arena.maybe_compact();
    }

    pub fn upsert_batch(&mut self, rows: Vec<NovelRow>) {
        if rows.is_empty() {
            return;
        }
        // Keep last row per id if caller sends duplicates.
        let mut dedup: Vec<NovelRow> = Vec::with_capacity(rows.len());
        let mut seen: FxHashMap<i64, usize> =
            FxHashMap::with_capacity_and_hasher(rows.len(), Default::default());
        for row in rows {
            if let Some(&pos) = seen.get(&row.id) {
                dedup[pos] = row;
            } else {
                seen.insert(row.id, dedup.len());
                dedup.push(row);
            }
        }

        let small_batch = dedup.len() <= 16;
        let mut bigram_scratch = Vec::with_capacity(256);

        if small_batch {
            for row in &dedup {
                let (doc, is_new) = self.alloc_doc(row.id);
                if !is_new {
                    let changed_mask = self.diff_numeric_mask(doc, row);
                    for field in NumericField::ALL {
                        if (changed_mask & field.bit()) != 0 {
                            self.remove_doc_from_track(field, doc);
                        }
                    }
                    self.clear_doc_postings(doc, &mut bigram_scratch, false);
                    self.write_row_into_doc(doc, row, false, &mut bigram_scratch);
                    for field in NumericField::ALL {
                        if (changed_mask & field.bit()) != 0 {
                            self.insert_doc_into_track(field, doc);
                        }
                    }
                } else {
                    self.write_row_into_doc(doc, row, false, &mut bigram_scratch);
                    for field in NumericField::ALL {
                        self.insert_doc_into_track(field, doc);
                    }
                }
            }
        } else {
            let mut dirty_mask = 0u16;
            for row in &dedup {
                if let Some(&doc) = self.id_to_doc.get(&row.id) {
                    dirty_mask |= self.diff_numeric_mask(doc, row);
                } else {
                    dirty_mask = u16::MAX;
                    break;
                }
            }
            self.invalidate_tracks(dirty_mask);
            for row in &dedup {
                let (doc, is_new) = self.alloc_doc(row.id);
                if !is_new {
                    self.clear_doc_postings(doc, &mut bigram_scratch, false);
                }
                self.write_row_into_doc(doc, row, false, &mut bigram_scratch);
            }
        }

        self.title_arena.maybe_compact();
        self.search_arena.maybe_compact();
    }

    fn diff_numeric_mask(&self, doc: u32, row: &NovelRow) -> u16 {
        let idx = doc as usize;
        let mut mask = 0u16;
        if (self.null_create_time.contains(doc), self.create_time_us[idx])
            != (row.create_time_us.is_none(), row.create_time_us.unwrap_or(0))
        {
            mask |= NumericField::CreateTime.bit();
        }
        if self.likes[idx] != row.like {
            mask |= NumericField::Like.bit();
        }
        if self.views[idx] != row.view {
            mask |= NumericField::View.bit();
        }
        if self.texts[idx] != row.text {
            mask |= NumericField::Text.bit();
        }
        if (self.null_author_id.contains(doc), self.author_id[idx])
            != (row.author_id.is_none(), row.author_id.unwrap_or(0))
        {
            mask |= NumericField::AuthorId.bit();
        }
        if (self.null_series_id.contains(doc), self.series_id[idx])
            != (row.series_id.is_none(), row.series_id.unwrap_or(0))
        {
            mask |= NumericField::SeriesId.bit();
        }
        if (self.null_series_index.contains(doc), self.series_index[idx])
            != (row.series_index.is_none(), row.series_index.unwrap_or(0))
        {
            mask |= NumericField::SeriesIndex.bit();
        }
        if self.has_epub[idx] != row.has_epub {
            mask |= NumericField::HasEpub.bit();
        }
        mask
    }

    pub fn toggle_special_follow(&mut self, author_id: i64, is_special_follow: bool) {
        if is_special_follow {
            self.special_follow_authors.insert(author_id);
            if let Some(bm) = self.author_bitmaps.get(&author_id) {
                self.sf_bitmap |= bm;
            }
        } else {
            self.special_follow_authors.remove(&author_id);
            if let Some(bm) = self.author_bitmaps.get(&author_id) {
                self.sf_bitmap -= bm;
            }
        }
    }

    pub fn set_blocked_tags(&mut self, blocked_tags: Vec<String>) {
        self.blocked_tag_names = blocked_tags.into_iter().collect();
        self.recompute_blocked_bitmap();
    }

    /// Resolve structural + 2-gram candidate bitmap and return `(mask, lower_segments)`!
    /// Caller still enforces inline `min_like`, `min_text`, and exact `memmem` verification.
    fn resolve_candidates(&self, filter: &QueryFilter) -> (CandidateMask, Vec<String>) {
        let lower_segments: Vec<String> = {
            let mut v = Vec::with_capacity(filter.keyword_segments.len());
            for seg in &filter.keyword_segments {
                let low = seg.to_ascii_lowercase();
                if !low.is_empty() && !v.contains(&low) {
                    v.push(low);
                }
            }
            v
        };

        let exclude_active = filter
            .exclude_blocked_tags
            .unwrap_or(self.exclude_blocked_default);

        // `count_blocked=True` with no exclusion or no blocked tags matches 0 rows (PG parity).
        if filter.count_blocked && (!exclude_active || self.blocked_bitmap.is_empty()) {
            return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
        }

        let mut mask = CandidateMask::AllAlive;

        if let Some(ref rids) = filter.restrict_ids {
            let mut bm = RoaringBitmap::new();
            for nid in rids {
                if let Some(&doc) = self.id_to_doc.get(nid) {
                    if self.alive.contains(doc) {
                        bm.insert(doc);
                    }
                }
            }
            if bm.is_empty() {
                return (CandidateMask::Bitmap(bm), lower_segments);
            }
            mask = CandidateMask::Bitmap(bm);
        }

        // Fast O(1) int filters
        for &(field, val) in &filter.int_filters {
            match field {
                IntFilterField::Id => {
                    let Some(&doc) = self.id_to_doc.get(&val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    };
                    if !self.alive.contains(doc) || !mask.contains(doc) {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    }
                    let mut bm = RoaringBitmap::new();
                    bm.insert(doc);
                    mask = CandidateMask::Bitmap(bm);
                }
                IntFilterField::AuthorId => {
                    let Some(bm) = self.author_bitmaps.get(&val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    };
                    mask.intersect_bm(bm);
                }
                IntFilterField::SeriesId => {
                    let Some(bm) = self.series_bitmaps.get(&val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    };
                    mask.intersect_bm(bm);
                }
                IntFilterField::HasEpub => {
                    let Ok(val_i32) = i32::try_from(val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    };
                    let Some(bm) = self.epub_bitmaps.get(&val_i32) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    };
                    mask.intersect_bm(bm);
                }
                IntFilterField::Like
                | IntFilterField::View
                | IntFilterField::Text
                | IntFilterField::SeriesIndex => {}
            }
            if mask.is_empty(&self.alive) {
                return (mask, lower_segments);
            }
        }

        if filter.only_favourite {
            mask.intersect_bm(&self.fav_bitmap);
            if mask.is_empty(&self.alive) {
                return (mask, lower_segments);
            }
        }
        if filter.only_special_follow {
            mask.intersect_bm(&self.sf_bitmap);
            if mask.is_empty(&self.alive) {
                return (mask, lower_segments);
            }
        }

        if filter.count_blocked {
            mask.intersect_bm(&self.blocked_bitmap);
            if mask.is_empty(&self.alive) {
                return (mask, lower_segments);
            }
        }

        for tag in &filter.tags {
            let Some(tid) = self.tag_interner.get_id(tag) else {
                return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
            };
            mask.intersect_bm(&self.tag_bitmaps[tid as usize]);
            if mask.is_empty(&self.alive) {
                return (mask, lower_segments);
            }
        }

        for (field, val) in &filter.str_filters {
            match field {
                StrFilterField::Source => {
                    let Some(id) = self.source_interner.get_id(val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    };
                    mask.intersect_bm(&self.source_bitmaps[id as usize]);
                }
                StrFilterField::AuthorName => {
                    let Some(id) = self.author_name_interner.get_id(val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    };
                    mask.intersect_bm(&self.author_name_bitmaps[id as usize]);
                }
                StrFilterField::SeriesName => {
                    let Some(id) = self.series_name_interner.get_id(val) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    };
                    mask.intersect_bm(&self.series_name_bitmaps[id as usize]);
                }
                StrFilterField::Title => {
                    let th = hash_bytes(val.as_bytes());
                    let Some(bucket) = self.title_hash_buckets.get(&th) else {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    };
                    let mut exact = RoaringBitmap::new();
                    for doc in bucket.iter() {
                        if mask.contains(doc) && self.title_arena.get_str(doc) == val {
                            exact.insert(doc);
                        }
                    }
                    mask = CandidateMask::Bitmap(exact);
                }
            }
            if mask.is_empty(&self.alive) {
                return (mask, lower_segments);
            }
        }

        // Exact integer filters without dedicated hash maps (`like`, `view`, `text`, `series_index`).
        for &(field, val) in &filter.int_filters {
            match field {
                IntFilterField::Like => {
                    let iter: Box<dyn Iterator<Item = u32>> = match &mask {
                        CandidateMask::AllAlive => Box::new(self.alive.iter()),
                        CandidateMask::Bitmap(bm) => Box::new(bm.iter()),
                    };
                    mask = CandidateMask::Bitmap(
                        iter.filter(|&d| self.likes[d as usize] == val).collect(),
                    );
                }
                IntFilterField::View => {
                    let iter: Box<dyn Iterator<Item = u32>> = match &mask {
                        CandidateMask::AllAlive => Box::new(self.alive.iter()),
                        CandidateMask::Bitmap(bm) => Box::new(bm.iter()),
                    };
                    mask = CandidateMask::Bitmap(
                        iter.filter(|&d| self.views[d as usize] == val).collect(),
                    );
                }
                IntFilterField::Text => {
                    let iter: Box<dyn Iterator<Item = u32>> = match &mask {
                        CandidateMask::AllAlive => Box::new(self.alive.iter()),
                        CandidateMask::Bitmap(bm) => Box::new(bm.iter()),
                    };
                    mask = CandidateMask::Bitmap(
                        iter.filter(|&d| self.texts[d as usize] == val).collect(),
                    );
                }
                IntFilterField::SeriesIndex => {
                    let iter: Box<dyn Iterator<Item = u32>> = match &mask {
                        CandidateMask::AllAlive => Box::new(self.alive.iter()),
                        CandidateMask::Bitmap(bm) => Box::new(bm.iter()),
                    };
                    mask = CandidateMask::Bitmap(
                        iter.filter(|&d| {
                            !self.null_series_index.contains(d)
                                && self.series_index[d as usize] == val
                        })
                        .collect(),
                    );
                }
                _ => {}
            }
            if mask.is_empty(&self.alive) {
                return (mask, lower_segments);
            }
        }

        if !filter.count_blocked && exclude_active {
            mask.subtract_bm(&self.blocked_bitmap, &self.alive);
            if mask.is_empty(&self.alive) {
                return (mask, lower_segments);
            }
        }

        if let Some(ref ex) = filter.exclude_ids {
            if !ex.is_empty() {
                let mut ex_bm = RoaringBitmap::new();
                for nid in ex {
                    if let Some(&doc) = self.id_to_doc.get(nid) {
                        ex_bm.insert(doc);
                    }
                }
                mask.subtract_bm(&ex_bm, &self.alive);
                if mask.is_empty(&self.alive) {
                    return (mask, lower_segments);
                }
            }
        }

        // Byte 2-gram intersection across all keyword segments of byte len >= 2.
        if !lower_segments.is_empty() {
            let mut bg_list: Vec<(u64, u16)> = Vec::new();
            for seg in &lower_segments {
                let bytes = seg.as_bytes();
                if bytes.len() < 2 {
                    continue;
                }
                for w in bytes.windows(2) {
                    let bg = ((w[0] as u16) << 8) | (w[1] as u16);
                    let len = self.bigrams[bg as usize].len();
                    if len == 0 {
                        return (CandidateMask::Bitmap(RoaringBitmap::new()), lower_segments);
                    }
                    bg_list.push((len, bg));
                }
            }
            if !bg_list.is_empty() {
                bg_list.sort_unstable();
                bg_list.dedup_by_key(|x| x.1);
                for (idx, &(_, bg)) in bg_list.iter().enumerate() {
                    mask.intersect_bm(&self.bigrams[bg as usize]);
                    let len = mask.len(&self.alive);
                    if len == 0 {
                        return (mask, lower_segments);
                    }
                    if idx >= 11 || len <= 32 {
                        break;
                    }
                }
            }
        }

        (mask, lower_segments)
    }

    #[inline]
    fn doc_passes_inline(
        &self,
        doc: u32,
        min_like: i64,
        min_text: i64,
        finders: &[Finder<'_>],
    ) -> bool {
        if min_like > 0 && self.likes[doc as usize] < min_like {
            return false;
        }
        if min_text > 0 && self.texts[doc as usize] < min_text {
            return false;
        }
        if finders.is_empty() {
            return true;
        }
        let hay = self.search_arena.get_bytes(doc);
        finders.iter().all(|f| f.find(hay).is_some())
    }

    fn verify_all_matching(
        &self,
        mask: &CandidateMask,
        min_like: i64,
        min_text: i64,
        lower_segments: &[String],
    ) -> RoaringBitmap {
        let finders: Vec<Finder<'_>> = lower_segments
            .iter()
            .map(|s| Finder::new(s.as_bytes()))
            .collect();

        let fast_unfiltered = finders.is_empty() && min_like <= 0 && min_text <= 0;
        if fast_unfiltered {
            return match mask {
                CandidateMask::AllAlive => self.alive.clone(),
                CandidateMask::Bitmap(bm) => bm.clone(),
            };
        }

        let docs: Vec<u32> = match mask {
            CandidateMask::AllAlive => self.alive.iter().collect(),
            CandidateMask::Bitmap(bm) => bm.iter().collect(),
        };

        if docs.len() >= 2048 && !finders.is_empty() {
            let chunk_matched: Vec<Vec<u32>> = docs
                .par_chunks(1024)
                .map(|chunk| {
                    let local_finders: Vec<Finder<'_>> = lower_segments
                        .iter()
                        .map(|s| Finder::new(s.as_bytes()))
                        .collect();
                    chunk
                        .iter()
                        .copied()
                        .filter(|&d| self.doc_passes_inline(d, min_like, min_text, &local_finders))
                        .collect()
                })
                .collect();
            let mut bm = RoaringBitmap::new();
            for part in chunk_matched {
                for d in part {
                    bm.push(d);
                }
            }
            bm
        } else {
            let mut bm = RoaringBitmap::new();
            for d in docs {
                if self.doc_passes_inline(d, min_like, min_text, &finders) {
                    bm.push(d);
                }
            }
            bm
        }
    }

    pub fn count_novels(&self, filter: &QueryFilter, cap: usize) -> (usize, bool) {
        let (mask, lower_segments) = self.resolve_candidates(filter);
        let min_like = filter.min_like.unwrap_or(0);
        let min_text = filter.min_text.unwrap_or(0);

        if lower_segments.is_empty() && min_like <= 0 && min_text <= 0 {
            let total = mask.len(&self.alive);
            let truncated = total > cap as u64;
            return ((total as usize).min(cap), truncated);
        }

        let finders: Vec<Finder<'_>> = lower_segments
            .iter()
            .map(|s| Finder::new(s.as_bytes()))
            .collect();

        let mut matched = 0usize;
        match &mask {
            CandidateMask::AllAlive => {
                for doc in self.alive.iter() {
                    if self.doc_passes_inline(doc, min_like, min_text, &finders) {
                        matched += 1;
                        if matched > cap {
                            return (cap, true);
                        }
                    }
                }
            }
            CandidateMask::Bitmap(bm) => {
                for doc in bm.iter() {
                    if self.doc_passes_inline(doc, min_like, min_text, &finders) {
                        matched += 1;
                        if matched > cap {
                            return (cap, true);
                        }
                    }
                }
            }
        }
        (matched, false)
    }

    pub fn has_match(&self, filter: &QueryFilter) -> bool {
        self.count_novels(filter, 0).1
    }

    pub fn list_matching_ids(&self, filter: &QueryFilter) -> Vec<i64> {
        let (mask, lower_segments) = self.resolve_candidates(filter);
        let min_like = filter.min_like.unwrap_or(0);
        let min_text = filter.min_text.unwrap_or(0);
        let bm = self.verify_all_matching(&mask, min_like, min_text, &lower_segments);
        let mut ids: Vec<i64> = bm.iter().map(|d| self.doc_ids[d as usize]).collect();
        ids.par_sort_unstable();
        ids
    }

    pub fn list_blocked_ids(&self) -> Vec<i64> {
        if !self.exclude_blocked_default || self.blocked_bitmap.is_empty() {
            return Vec::new();
        }
        let mut ids: Vec<i64> = self
            .blocked_bitmap
            .iter()
            .map(|d| self.doc_ids[d as usize])
            .collect();
        ids.sort_unstable();
        ids
    }

    pub fn sort_ids(
        &self,
        novel_ids: &[i64],
        axis: SortAxis,
        descending: bool,
    ) -> Vec<i64> {
        if novel_ids.is_empty() {
            return Vec::new();
        }
        let mut docs: Vec<u32> = Vec::with_capacity(novel_ids.len());
        let mut seen = FxHashSet::with_capacity_and_hasher(novel_ids.len(), Default::default());
        for &nid in novel_ids {
            if let Some(&doc) = self.id_to_doc.get(&nid) {
                if self.alive.contains(doc) && seen.insert(doc) {
                    docs.push(doc);
                }
            }
        }
        match axis {
            SortAxis::Numeric(field) => {
                docs.sort_unstable_by(|&a, &b| {
                    let ka = self.numeric_key(field, a);
                    let kb = self.numeric_key(field, b);
                    if descending {
                        ka.cmp(&kb)
                    } else {
                        kb.cmp(&ka)
                    }
                });
            }
            SortAxis::String(field) => {
                docs.sort_unstable_by(|&a, &b| {
                    let (na, va) = self.string_val(field, a);
                    let (nb, vb) = self.string_val(field, b);
                    let ia = StringRankItem {
                        descending,
                        is_null: na,
                        val: va,
                        novel_id: self.doc_ids[a as usize],
                        doc: a,
                    };
                    let ib = StringRankItem {
                        descending,
                        is_null: nb,
                        val: vb,
                        novel_id: self.doc_ids[b as usize],
                        doc: b,
                    };
                    ia.cmp(&ib)
                });
            }
            SortAxis::None | SortAxis::Random => {}
        }
        docs.into_iter().map(|d| self.doc_ids[d as usize]).collect()
    }

    fn materialize_doc(&self, doc: u32) -> NovelRow {
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
        let series_index = (!self.null_series_index.contains(doc)).then(|| self.series_index[idx]);
        let text = self.texts[idx];
        let like = self.likes[idx];
        let view = self.views[idx];
        let has_epub = self.has_epub[idx];
        let create_time_us = (!self.null_create_time.contains(doc)).then(|| self.create_time_us[idx]);
        let src_sid = self.doc_source[idx];
        let source = if src_sid != StringInterner::NONE {
            self.source_interner.get_str(src_sid).to_owned()
        } else {
            "pixiv".to_string()
        };
        let is_favourite = self.fav_bitmap.contains(doc);
        let is_special_follow = self.sf_bitmap.contains(doc);
        let tags = self.doc_tags[idx]
            .iter()
            .map(|&tid| self.tag_interner.get_str(tid).to_owned())
            .collect();
        NovelRow {
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

    pub fn get_novel_rows_by_ids(&self, novel_ids: &[i64]) -> Vec<NovelRow> {
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

    pub fn query_novels(
        &self,
        filter: &QueryFilter,
        axis: SortAxis,
        descending: bool,
        limit: usize,
        cursor: Option<&TrackCursor>,
    ) -> Result<(Vec<i64>, bool, Option<(String, String, i64)>), String> {
        let (slots, has_more, rand_out) =
            self.query_novels_slots(filter, axis, descending, limit, cursor)?;
        let ids = slots.into_iter().map(|d| self.doc_ids[d as usize]).collect();
        Ok((ids, has_more, rand_out))
    }

    pub fn query_novel_rows(
        &self,
        filter: &QueryFilter,
        axis: SortAxis,
        descending: bool,
        limit: usize,
        cursor: Option<&TrackCursor>,
    ) -> Result<(Vec<NovelRow>, bool, Option<(String, String, i64)>), String> {
        let (slots, has_more, rand_out) =
            self.query_novels_slots(filter, axis, descending, limit, cursor)?;
        let rows = slots.into_iter().map(|d| self.materialize_doc(d)).collect();
        Ok((rows, has_more, rand_out))
    }

    /// Execute paginated `/novels` query (`order_by` already parsed).
    /// Returns `(doc_slots_truncated_to_limit, has_more, random_cursor_if_more)`.
    fn query_novels_slots(
        &self,
        filter: &QueryFilter,
        axis: SortAxis,
        descending: bool,
        limit: usize,
        cursor: Option<&TrackCursor>,
    ) -> Result<(Vec<u32>, bool, Option<(String, String, i64)>), String> {
        let (mask, lower_segments) = self.resolve_candidates(filter);
        if mask.is_empty(&self.alive) {
            return Ok((Vec::new(), false, None));
        }

        let min_like = filter.min_like.unwrap_or(0);
        let min_text = filter.min_text.unwrap_or(0);

        match axis {
            SortAxis::Random => {
                let (seed, rand_cursor) = match cursor {
                    Some(TrackCursor::Random(s, h, cid)) => (*s, Some((*h, *cid))),
                    None => (fresh_seed(), None),
                    Some(_) => return Err("Expected Random cursor".into()),
                };

                let finders: Vec<Finder<'_>> = lower_segments
                    .iter()
                    .map(|s| Finder::new(s.as_bytes()))
                    .collect();

                let use_parallel = limit > 0
                    && match &mask {
                        CandidateMask::AllAlive => self.alive.len() >= 8192,
                        CandidateMask::Bitmap(bm) => bm.len() >= 8192,
                    };

                let (hits, has_more) = if use_parallel {
                    top_k_random_parallel(
                        self.doc_ids.len() as u32,
                        &self.doc_ids,
                        seed,
                        rand_cursor,
                        limit,
                        |d| {
                            mask.contains_slot(d, &self.alive)
                                && self.doc_passes_inline(d, min_like, min_text, &finders)
                        },
                    )
                } else {
                    match &mask {
                        CandidateMask::AllAlive => top_k_random_bitmap(
                            &self.alive,
                            &self.doc_ids,
                            seed,
                            rand_cursor,
                            limit,
                            |d| self.doc_passes_inline(d, min_like, min_text, &finders),
                        ),
                        CandidateMask::Bitmap(bm) => top_k_random_bitmap(
                            bm,
                            &self.doc_ids,
                            seed,
                            rand_cursor,
                            limit,
                            |d| self.doc_passes_inline(d, min_like, min_text, &finders),
                        ),
                    }
                };

                let rand_out = if has_more && !hits.is_empty() {
                    let (last_h, last_id, _) = hits[hits.len() - 1];
                    Some((hex16_encode(seed), hex16_encode(last_h), last_id))
                } else {
                    None
                };
                let slots = hits.into_iter().map(|(_, _, doc)| doc).collect();
                Ok((slots, has_more, rand_out))
            }
            SortAxis::None => {
                if cursor.is_some() {
                    return Err("Invalid order_by for cursor pagination: none".into());
                }
                let finders: Vec<Finder<'_>> = lower_segments
                    .iter()
                    .map(|s| Finder::new(s.as_bytes()))
                    .collect();
                let mut slots = Vec::with_capacity(limit + 1);
                match &mask {
                    CandidateMask::AllAlive => {
                        for d in self.alive.iter() {
                            if self.doc_passes_inline(d, min_like, min_text, &finders) {
                                slots.push(d);
                                if slots.len() > limit {
                                    break;
                                }
                            }
                        }
                    }
                    CandidateMask::Bitmap(bm) => {
                        for d in bm.iter() {
                            if self.doc_passes_inline(d, min_like, min_text, &finders) {
                                slots.push(d);
                                if slots.len() > limit {
                                    break;
                                }
                            }
                        }
                    }
                }
                let has_more = slots.len() > limit;
                slots.truncate(limit);
                Ok((slots, has_more, None))
            }
            SortAxis::Numeric(field) => {
                let cursor_int = match cursor {
                    Some(TrackCursor::Int(v, cid)) => {
                        let cmp_val = if field == NumericField::AbsId {
                            cid.unsigned_abs() as i128
                        } else {
                            *v as i128
                        };
                        Some((cmp_val, *cid))
                    }
                    None => None,
                    Some(_) => return Err("Expected integer/datetime cursor".into()),
                };

                let finders: Vec<Finder<'_>> = lower_segments
                    .iter()
                    .map(|s| Finder::new(s.as_bytes()))
                    .collect();

                // Fast bounded top-K path for small filtered subsets avoids scanning full track.
                if let CandidateMask::Bitmap(bm) = &mask {
                    if bm.len() <= 4096 {
                        let iter = bm.iter().filter_map(|doc| {
                            if !self.doc_passes_inline(doc, min_like, min_text, &finders) {
                                return None;
                            }
                            if let Some((cval, cid)) = cursor_int {
                                if self.field_is_null(field, doc) {
                                    return None;
                                }
                                let dval = self.numeric_val_i128(field, doc);
                                let did = self.doc_ids[doc as usize];
                                let ok = if descending {
                                    (dval, did) < (cval, cid)
                                } else {
                                    (dval, did) > (cval, cid)
                                };
                                if !ok {
                                    return None;
                                }
                            }
                            Some(NumericRankItem {
                                descending,
                                key: self.numeric_key(field, doc),
                                novel_id: self.doc_ids[doc as usize],
                                doc,
                            })
                        });
                        let (hits, has_more) = collect_top_k(iter, limit);
                        return Ok((hits.into_iter().map(|h| h.doc).collect(), has_more, None));
                    }
                }

                // Walk canonical pre-sorted track (`DESC NULLS FIRST, id DESC`) using O(log N) seek.
                let track = &self.tracks[field as usize];
                let mut out = Vec::with_capacity(limit + 1);
                if descending {
                    let start = match cursor_int {
                        None => 0,
                        Some((cval, cid)) => track.partition_point(|&doc| {
                            if self.field_is_null(field, doc) {
                                return true;
                            }
                            let dval = self.numeric_val_i128(field, doc);
                            let did = self.doc_ids[doc as usize];
                            dval > cval || (dval == cval && did >= cid)
                        }),
                    };
                    for &doc in &track[start..] {
                        if mask.contains(doc)
                            && self.doc_passes_inline(doc, min_like, min_text, &finders)
                        {
                            out.push(doc);
                            if out.len() > limit {
                                break;
                            }
                        }
                    }
                } else {
                    let null_end = track.partition_point(|&doc| self.field_is_null(field, doc));
                    match cursor_int {
                        Some((cval, cid)) => {
                            let end = track.partition_point(|&doc| {
                                if self.field_is_null(field, doc) {
                                    return true;
                                }
                                let dval = self.numeric_val_i128(field, doc);
                                let did = self.doc_ids[doc as usize];
                                dval > cval || (dval == cval && did > cid)
                            });
                            for &doc in track[null_end..end].iter().rev() {
                                if mask.contains(doc)
                                    && self.doc_passes_inline(doc, min_like, min_text, &finders)
                                {
                                    out.push(doc);
                                    if out.len() > limit {
                                        break;
                                    }
                                }
                            }
                        }
                        None => {
                            'asc_outer: {
                                for &doc in track[null_end..].iter().rev() {
                                    if mask.contains(doc)
                                        && self.doc_passes_inline(doc, min_like, min_text, &finders)
                                    {
                                        out.push(doc);
                                        if out.len() > limit {
                                            break 'asc_outer;
                                        }
                                    }
                                }
                                for &doc in track[..null_end].iter().rev() {
                                    if mask.contains(doc)
                                        && self.doc_passes_inline(doc, min_like, min_text, &finders)
                                    {
                                        out.push(doc);
                                        if out.len() > limit {
                                            break 'asc_outer;
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                let has_more = out.len() > limit;
                out.truncate(limit);
                Ok((out, has_more, None))
            }
            SortAxis::String(field) => {
                let cursor_str = match cursor {
                    Some(TrackCursor::Str(s, cid)) => Some((s.as_str(), *cid)),
                    None => None,
                    Some(_) => return Err("Expected string cursor".into()),
                };
                let bm = self.verify_all_matching(&mask, min_like, min_text, &lower_segments);
                let iter = bm.iter().filter_map(|doc| {
                    let (is_null, val) = self.string_val(field, doc);
                    let nid = self.doc_ids[doc as usize];
                    if let Some((cstr, cid)) = cursor_str {
                        if is_null {
                            return None;
                        }
                        let ok = if descending {
                            (val, nid) < (cstr, cid)
                        } else {
                            (val, nid) > (cstr, cid)
                        };
                        if !ok {
                            return None;
                        }
                    }
                    Some(StringRankItem {
                        descending,
                        is_null,
                        val,
                        novel_id: nid,
                        doc,
                    })
                });
                let (hits, has_more) = collect_top_k(iter, limit);
                Ok((hits.into_iter().map(|h| h.doc).collect(), has_more, None))
            }
        }
    }
}

#[inline]
fn insert_bm(bm: &mut RoaringBitmap, doc: u32, sequential: bool) {
    if sequential {
        bm.push(doc);
    } else {
        bm.insert(doc);
    }
}

#[inline]
fn intern_posting(
    interner: &mut StringInterner,
    bitmaps: &mut Vec<RoaringBitmap>,
    value: &str,
    doc: u32,
    sequential: bool,
) -> u32 {
    let id = interner.intern(value);
    if id as usize >= bitmaps.len() {
        bitmaps.resize_with(id as usize + 1, RoaringBitmap::new);
    }
    insert_bm(&mut bitmaps[id as usize], doc, sequential);
    id
}

/// Staged snapshot builder used at startup / full reload without holding engine lock.
pub struct StagingBuilder {
    pub state: EngineState,
    bigram_scratch: Vec<u16>,
    expected_sequential: u32,
}

impl StagingBuilder {
    pub fn new(capacity: usize) -> Self {
        Self {
            state: EngineState::with_capacity(capacity),
            bigram_scratch: Vec::with_capacity(256),
            expected_sequential: 0,
        }
    }

    pub fn push_batch(&mut self, rows: Vec<NovelRow>) {
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
            self.state.doc_tags.push(Vec::new());
            self.state.doc_source.push(StringInterner::NONE);
            self.state.doc_author_name.push(StringInterner::NONE);
            self.state.doc_series_name.push(StringInterner::NONE);
            self.state
                .write_row_into_doc(doc, row, true, &mut self.bigram_scratch);
        }
    }

    pub fn finalize(
        mut self,
        blocked_tags: Vec<String>,
        exclude_blocked_default: bool,
        special_follow_authors: Vec<i64>,
    ) -> EngineState {
        self.state.blocked_tag_names = blocked_tags.into_iter().collect();
        self.state.exclude_blocked_default = exclude_blocked_default;
        for aid in special_follow_authors {
            self.state.special_follow_authors.insert(aid);
            if let Some(bm) = self.state.author_bitmaps.get(&aid) {
                self.state.sf_bitmap |= bm;
            }
        }
        self.state.recompute_blocked_bitmap();

        // Build hot numeric tracks in parallel on staging prior to atomic swap.
        let built_tracks: Vec<(NumericField, Vec<u32>)> = NumericField::HOT
            .par_iter()
            .map(|&field| {
                let mut keyed: Vec<(CanonNumKey, u32)> = self
                    .state
                    .alive
                    .iter()
                    .map(|doc| (self.state.numeric_key(field, doc), doc))
                    .collect();
                keyed.par_sort_unstable_by(|a, b| a.0.cmp(&b.0));
                (field, keyed.into_iter().map(|(_, d)| d).collect())
            })
            .collect();
        for (field, track) in built_tracks {
            let idx = field as usize;
            self.state.tracks[idx] = track;
            self.state.tracks_dirty[idx] = false;
        }
        self.state.initialized = true;
        self.state
    }
}

pub struct NovelIndex {
    inner: RwLock<EngineState>,
}

impl NovelIndex {
    pub fn new() -> Self {
        Self {
            inner: RwLock::new(EngineState::default()),
        }
    }

    pub fn ready(&self) -> bool {
        self.inner.read().initialized
    }

    pub fn stats(&self) -> (usize, usize, usize, bool) {
        let s = self.inner.read();
        (
            s.alive.len() as usize,
            s.doc_ids.len(),
            s.blocked_bitmap.len() as usize,
            s.exclude_blocked_default,
        )
    }

    pub fn swap_state(&self, next: EngineState) {
        let mut guard = self.inner.write();
        *guard = next;
    }

    pub fn upsert_batch(&self, rows: Vec<NovelRow>) {
        self.inner.write().upsert_batch(rows);
    }

    pub fn delete_ids(&self, novel_ids: &[i64]) {
        self.inner.write().delete_ids(novel_ids);
    }

    pub fn toggle_special_follow(&self, author_id: i64, is_special_follow: bool) {
        self.inner
            .write()
            .toggle_special_follow(author_id, is_special_follow);
    }

    pub fn set_blocked_tags(&self, blocked_tags: Vec<String>) {
        self.inner.write().set_blocked_tags(blocked_tags);
    }

    pub fn set_exclude_blocked_default(&self, value: bool) {
        self.inner.write().exclude_blocked_default = value;
    }

    fn ensure_numeric_track_and_read<'a>(&'a self, field: NumericField) -> parking_lot::RwLockReadGuard<'a, EngineState> {
        {
            let r = self.inner.read();
            if !r.tracks_dirty[field as usize] {
                return r;
            }
        }
        {
            let mut w = self.inner.write();
            if w.tracks_dirty[field as usize] {
                w.rebuild_track(field);
            }
        }
        // Note: a concurrent writer could theoretically invalidate right between unlock/read; loop handles safely.
        loop {
            let r = self.inner.read();
            if !r.tracks_dirty[field as usize] {
                return r;
            }
            drop(r);
            let mut w = self.inner.write();
            if w.tracks_dirty[field as usize] {
                w.rebuild_track(field);
            }
        }
    }

    pub fn query_novels(
        &self,
        filter: &QueryFilter,
        axis: SortAxis,
        descending: bool,
        limit: usize,
        cursor: Option<&TrackCursor>,
    ) -> Result<(Vec<i64>, bool, Option<(String, String, i64)>), String> {
        match axis {
            SortAxis::Numeric(field) => {
                let guard = self.ensure_numeric_track_and_read(field);
                guard.query_novels(filter, axis, descending, limit, cursor)
            }
            _ => {
                let guard = self.inner.read();
                guard.query_novels(filter, axis, descending, limit, cursor)
            }
        }
    }

    pub fn query_novel_rows(
        &self,
        filter: &QueryFilter,
        axis: SortAxis,
        descending: bool,
        limit: usize,
        cursor: Option<&TrackCursor>,
    ) -> Result<(Vec<NovelRow>, bool, Option<(String, String, i64)>), String> {
        match axis {
            SortAxis::Numeric(field) => {
                let guard = self.ensure_numeric_track_and_read(field);
                guard.query_novel_rows(filter, axis, descending, limit, cursor)
            }
            _ => {
                let guard = self.inner.read();
                guard.query_novel_rows(filter, axis, descending, limit, cursor)
            }
        }
    }

    pub fn get_novel_rows_by_ids(&self, novel_ids: &[i64]) -> Vec<NovelRow> {
        self.inner.read().get_novel_rows_by_ids(novel_ids)
    }

    pub fn count_novels(&self, filter: &QueryFilter, cap: usize) -> (usize, bool) {
        self.inner.read().count_novels(filter, cap)
    }

    pub fn has_match(&self, filter: &QueryFilter) -> bool {
        self.inner.read().has_match(filter)
    }

    pub fn list_matching_ids(&self, filter: &QueryFilter) -> Vec<i64> {
        self.inner.read().list_matching_ids(filter)
    }

    pub fn list_blocked_ids(&self) -> Vec<i64> {
        self.inner.read().list_blocked_ids()
    }

    pub fn sort_ids(&self, novel_ids: &[i64], axis: SortAxis, descending: bool) -> Vec<i64> {
        self.inner.read().sort_ids(novel_ids, axis, descending)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::random::hex16_decode;

    fn sample_row(id: i64, title: &str, tags: Vec<&str>) -> NovelRow {
        NovelRow {
            id,
            title: title.to_string(),
            author_id: Some(10),
            author_name: Some("AuthorA".to_string()),
            series_id: None,
            series_name: None,
            series_index: None,
            text: 1000,
            like: id.abs() * 10,
            view: id.abs() * 100,
            has_epub: 0,
            create_time_us: Some(id.abs() * 1_000_000),
            source: "pixiv".to_string(),
            is_favourite: false,
            is_special_follow: false,
            tags: tags.into_iter().map(str::to_string).collect(),
        }
    }

    #[test]
    fn search_text_ascii_only_fold_and_whitespace() {
        let s = build_search_lower("Hello Ñ World", Some("Bob"), None, &["原神".into(), "A".into()]);
        assert_eq!(s, "hello Ñ world bob 原神 a");

        let mut b = StagingBuilder::new(4);
        b.push_batch(vec![
            sample_row(1, "Hello Ñ Test", vec!["原神", "百合"]),
            sample_row(2, "ñ lower only", vec!["原神"]),
        ]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![]));

        // Query uppercase 'Ñ' must ONLY match row 1 (PG C-locale parity)
        let f = QueryFilter {
            keyword_segments: vec!["hello".into(), "Ñ".into()],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f), vec![1]);

        let f_1char = QueryFilter {
            keyword_segments: vec!["原".into()],
            ..Default::default()
        };
        assert_eq!(engine.list_matching_ids(&f_1char), vec![1, 2]);
    }

    #[test]
    fn abs_id_time_axis_and_nulls_order() {
        let mut r1 = sample_row(-200, "Local newer", vec![]);
        r1.create_time_us = None;
        let mut r2 = sample_row(100, "Pixiv older", vec![]);
        r2.create_time_us = Some(500);
        let mut r3 = sample_row(300, "Pixiv newest", vec![]);
        r3.create_time_us = Some(900);
        let mut r4 = sample_row(200, "Pixiv tie abs 200", vec![]);
        r4.create_time_us = None;

        let mut b = StagingBuilder::new(4);
        b.push_batch(vec![r1, r2, r3, r4]);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![]));

        // abs(id) DESC, id DESC -> 300, +200, -200, 100
        let f = QueryFilter::default();
        let (desc_ids, _, _) = engine
            .query_novels(&f, SortAxis::Numeric(NumericField::AbsId), true, 10, None)
            .unwrap();
        assert_eq!(desc_ids, vec![300, 200, -200, 100]);

        // abs(id) ASC, id ASC -> 100, -200, +200, 300
        let (asc_ids, _, _) = engine
            .query_novels(&f, SortAxis::Numeric(NumericField::AbsId), false, 10, None)
            .unwrap();
        assert_eq!(asc_ids, vec![100, -200, 200, 300]);

        // create_time DESC NULLS FIRST, id DESC -> (+200, -200, 300, 100)
        let (ct_desc, _, _) = engine
            .query_novels(&f, SortAxis::Numeric(NumericField::CreateTime), true, 10, None)
            .unwrap();
        assert_eq!(ct_desc, vec![200, -200, 300, 100]);

        // create_time ASC NULLS LAST, id ASC -> (100, 300, -200, +200)
        let (ct_asc, _, _) = engine
            .query_novels(&f, SortAxis::Numeric(NumericField::CreateTime), false, 10, None)
            .unwrap();
        assert_eq!(ct_asc, vec![100, 300, -200, 200]);

        // Cursor non-null on create_time excludes NULL rows on both ASC & DESC
        let c = TrackCursor::Int(900, 300);
        let (after_desc, _, _) = engine
            .query_novels(&f, SortAxis::Numeric(NumericField::CreateTime), true, 10, Some(&c))
            .unwrap();
        assert_eq!(after_desc, vec![100]);

        let c_asc = TrackCursor::Int(500, 100);
        let (after_asc, _, _) = engine
            .query_novels(&f, SortAxis::Numeric(NumericField::CreateTime), false, 10, Some(&c_asc))
            .unwrap();
        assert_eq!(after_asc, vec![300]);
    }

    #[test]
    fn incremental_updates_deletes_and_random_pagination() {
        let rows: Vec<NovelRow> = (1..=35).map(|i| sample_row(i, &format!("Book {i}"), vec!["T1"])).collect();
        let mut b = StagingBuilder::new(35);
        b.push_batch(rows);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec!["T1".into()], true, vec![]));

        // Blocked tag active -> 0 visible, 35 excluded
        let f = QueryFilter::default();
        assert_eq!(engine.count_novels(&f, 5000), (0, false));
        let f_ex = QueryFilter { count_blocked: true, ..Default::default() };
        assert!(engine.has_match(&f_ex));
        assert_eq!(engine.count_novels(&f_ex, 5000), (35, false));

        // Unblock T1
        engine.set_blocked_tags(vec![]);
        assert_eq!(engine.count_novels(&f, 5000), (35, false));

        // Update id=5 like & title (small batch maintains clean tracks in place)
        let mut mod5 = sample_row(5, "Renamed Five", vec!["T2"]);
        mod5.like = 999999;
        engine.upsert_batch(vec![mod5]);
        let (top1, has_more, _) = engine
            .query_novels(&f, SortAxis::Numeric(NumericField::Like), true, 1, None)
            .unwrap();
        assert_eq!(top1, vec![5]);
        assert!(has_more);

        // Delete id=5 and re-insert id=5 with slot reuse
        engine.delete_ids(&[5]);
        assert_eq!(engine.count_novels(&f, 5000), (34, false));
        let mut mod5_again = sample_row(5, "Reinserted", vec![]);
        mod5_again.like = 1;
        engine.upsert_batch(vec![mod5_again]);
        assert_eq!(engine.count_novels(&f, 5000), (35, false));

        // Exhaustive stateless random cursor walk collects all 35 unique IDs
        let mut seen = FxHashSet::default();
        let mut cur: Option<TrackCursor> = None;
        loop {
            let (ids, more, next_rand) = engine
                .query_novels(&f, SortAxis::Random, true, 10, cur.as_ref())
                .unwrap();
            for id in ids {
                assert!(seen.insert(id), "duplicate id {id}");
            }
            if !more {
                assert!(next_rand.is_none());
                break;
            }
            let (sh, hh, lid) = next_rand.unwrap();
            cur = Some(TrackCursor::Random(
                hex16_decode(&sh).unwrap(),
                hex16_decode(&hh).unwrap(),
                lid,
            ));
        }
        assert_eq!(seen.len(), 35);
    }

    #[test]
    fn incremental_clean_tracks_match_full_rebuild() {
        let mut rows: Vec<NovelRow> = Vec::new();
        for i in 1..=25i64 {
            let mut r = sample_row(if i % 5 == 0 { -i } else { i }, &format!("N{i}"), vec!["T"]);
            if i % 3 == 0 {
                r.create_time_us = None;
            }
            if i % 4 == 0 {
                r.series_index = Some(i % 7);
            }
            rows.push(r);
        }
        let mut b = StagingBuilder::new(30);
        b.push_batch(rows);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![]));

        // Touch all cold tracks too so all 9 tracks are clean.
        let f = QueryFilter::default();
        for field in NumericField::ALL {
            let _ = engine
                .query_novels(&f, SortAxis::Numeric(field), true, 100, None)
                .unwrap();
        }

        // Perform <16 mutations exercising in-place track insert/update/delete.
        engine.delete_ids(&[-5, 10]);
        let mut u1 = sample_row(3, "Updated3", vec!["X"]);
        u1.like = 99999;
        u1.create_time_us = Some(42);
        let mut n1 = sample_row(-100, "NewNeg", vec!["Y"]);
        n1.create_time_us = None;
        let mut n2 = sample_row(100, "NewPos", vec!["Z"]);
        n2.create_time_us = None;
        engine.upsert_batch(vec![u1, n1, n2]);

        let state = engine.inner.read();
        for &field in &NumericField::ALL {
            assert!(!state.tracks_dirty[field as usize], "track {field:?} should stay clean");
            let mut expected: Vec<(CanonNumKey, u32)> = state
                .alive
                .iter()
                .map(|d| (state.numeric_key(field, d), d))
                .collect();
            expected.sort_unstable_by(|a, b| a.0.cmp(&b.0));
            let expected_docs: Vec<u32> = expected.into_iter().map(|(_, d)| d).collect();
            assert_eq!(state.tracks[field as usize], expected_docs, "mismatch on {field:?}");
        }
    }

    #[test]
    fn parallel_random_top_k_and_direct_row_materialization() {
        let mut rows: Vec<NovelRow> = (1..=10_000i64)
            .map(|i| {
                let mut r = sample_row(i, &format!("Book a {i}"), vec!["T1"]);
                r.author_name = Some(format!("Auth{}", i % 17));
                r.series_id = Some(i % 23);
                r.series_name = Some(format!("Ser{}", i % 23));
                r.series_index = Some((i % 9) + 1);
                r.has_epub = (i % 4) as i32;
                r.source = if i % 10 == 0 { "local".into() } else { "pixiv".into() };
                r.is_favourite = i % 7 == 0;
                r.is_special_follow = i % 11 == 0;
                r
            })
            .collect();
        // Force slot 0 deletion & check parallel random skips deleted slots properly
        rows.push(sample_row(-42, "Local a -42", vec!["T2"]));
        let mut b = StagingBuilder::new(rows.len());
        b.push_batch(rows);
        let engine = NovelIndex::new();
        engine.swap_state(b.finalize(vec![], true, vec![]));
        engine.delete_ids(&[1, 2, 5000]);

        // Test parallel random with 1-char keyword 'a' + cursor parity against sequential bitmap
        let f = QueryFilter {
            keyword_segments: vec!["a".into()],
            ..Default::default()
        };
        let state = engine.inner.read();
        let (mask, segs) = state.resolve_candidates(&f);
        let finders: Vec<Finder<'_>> = segs.iter().map(|s| Finder::new(s.as_bytes())).collect();
        let seed = 0xDEAD_BEEF_1234_5678u64;
        let (seq_hits, seq_more) =
            top_k_random_bitmap(&state.alive, &state.doc_ids, seed, None, 20, |d| {
                state.doc_passes_inline(d, 0, 0, &finders)
            });
        let (par_hits, par_more) = top_k_random_parallel(
            state.doc_ids.len() as u32,
            &state.doc_ids,
            seed,
            None,
            20,
            |d| mask.contains_slot(d, &state.alive) && state.doc_passes_inline(d, 0, 0, &finders),
        );
        assert_eq!(par_more, seq_more);
        assert_eq!(par_hits, seq_hits);
        drop(state);

        // Test direct row materialization matches query order & drops deleted ids
        let (mat_rows, has_more, _) = engine
            .query_novel_rows(&f, SortAxis::Random, true, 15, None)
            .unwrap();
        assert_eq!(mat_rows.len(), 15);
        assert!(has_more);
        let by_id = engine.get_novel_rows_by_ids(&[5000, -42, 77, 999999, 1]);
        assert_eq!(
            by_id.iter().map(|r| r.id).collect::<Vec<_>>(),
            vec![-42, 77],
        );
        assert_eq!(by_id[0].title, "Local a -42");
        assert_eq!(by_id[1].is_favourite, true);
        assert_eq!(by_id[1].is_special_follow, true);
        assert_eq!(by_id[1].author_id, Some(10));
    }
}
