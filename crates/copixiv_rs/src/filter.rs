use std::cmp::{Ordering, Reverse};

/// Hot numeric sort axes kept pre-sorted in `EngineState::tracks`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HotTrack {
    AbsId = 0,
    CreateTime = 1,
    Like = 2,
    View = 3,
    Text = 4,
}

impl HotTrack {
    pub const COUNT: usize = 5;

    pub const ALL: [HotTrack; Self::COUNT] = [
        Self::AbsId,
        Self::CreateTime,
        Self::Like,
        Self::View,
        Self::Text,
    ];

    #[inline]
    pub const fn bit(self) -> u8 {
        1u8 << (self as usize)
    }

    #[inline]
    pub const fn field(self) -> NumericField {
        match self {
            Self::AbsId => NumericField::AbsId,
            Self::CreateTime => NumericField::CreateTime,
            Self::Like => NumericField::Like,
            Self::View => NumericField::View,
            Self::Text => NumericField::Text,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NumericField {
    AbsId = 0,
    CreateTime = 1,
    Like = 2,
    View = 3,
    Text = 4,
}

impl NumericField {
    #[inline]
    pub const fn hot_track(self) -> HotTrack {
        match self {
            Self::AbsId => HotTrack::AbsId,
            Self::CreateTime => HotTrack::CreateTime,
            Self::Like => HotTrack::Like,
            Self::View => HotTrack::View,
            Self::Text => HotTrack::Text,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SortAxis {
    Numeric(NumericField),
    Random,
    None,
}

impl SortAxis {
    pub fn parse(name: &str) -> Result<Self, String> {
        match name {
            "id" => Ok(Self::Numeric(NumericField::AbsId)),
            "create_time" => Ok(Self::Numeric(NumericField::CreateTime)),
            "like" => Ok(Self::Numeric(NumericField::Like)),
            "view" => Ok(Self::Numeric(NumericField::View)),
            "text" => Ok(Self::Numeric(NumericField::Text)),
            "random" => Ok(Self::Random),
            "none" | "" => Ok(Self::None),
            other => Err(format!("Unsupported order_by field: {other}")),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IntFilterField {
    Id,
    AuthorId,
    SeriesId,
    SeriesIndex,
    Text,
    Like,
    View,
    HasEpub,
}

impl IntFilterField {
    pub fn parse(name: &str) -> Result<Self, String> {
        match name {
            "id" => Ok(Self::Id),
            "author_id" => Ok(Self::AuthorId),
            "series_id" => Ok(Self::SeriesId),
            "series_index" => Ok(Self::SeriesIndex),
            "text" => Ok(Self::Text),
            "like" => Ok(Self::Like),
            "view" => Ok(Self::View),
            "has_epub" => Ok(Self::HasEpub),
            other => Err(format!("Unsupported int filter field: {other}")),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StrFilterField {
    Title,
    AuthorName,
    SeriesName,
    Source,
}

impl StrFilterField {
    pub fn parse(name: &str) -> Result<Self, String> {
        match name {
            "title" => Ok(Self::Title),
            "author_name" => Ok(Self::AuthorName),
            "series_name" => Ok(Self::SeriesName),
            "source" => Ok(Self::Source),
            other => Err(format!("Unsupported str filter field: {other}")),
        }
    }
}

#[derive(Default, Clone, Debug)]
pub struct QueryFilter {
    pub tags: Vec<String>,
    pub keyword_segments: Vec<String>,
    pub int_filters: Vec<(IntFilterField, i64)>,
    pub str_filters: Vec<(StrFilterField, String)>,
    pub only_favourite: bool,
    pub only_special_follow: bool,
    pub min_like: Option<i64>,
    pub min_text: Option<i64>,
    /// `None` uses `EngineState::exclude_blocked_default`; `Some(b)` overrides it.
    pub exclude_blocked_tags: Option<bool>,
    /// When `true`, restrict matches ONLY to novels carrying blocked tags.
    pub count_blocked: bool,
    pub restrict_ids: Option<Vec<i64>>,
    pub exclude_ids: Option<Vec<i64>>,
}

#[derive(Clone, Debug)]
pub enum TrackCursor {
    Int(Option<i64>, i64),
    Random(u64, u64, i64), // (seed, h, id)
}

/// Canonical numeric sort key in `DESC NULLS FIRST, id DESC` order:
/// smaller `(Reverse(is_null), Reverse(val_i128), Reverse(id))` sorts EARLIER.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct CanonNumKey {
    pub is_null_rev: Reverse<bool>,
    pub val_rev: Reverse<i128>,
    pub id_rev: Reverse<i64>,
}

impl CanonNumKey {
    #[inline]
    pub fn from_val(is_null: bool, val: i128, novel_id: i64) -> Self {
        Self {
            is_null_rev: Reverse(is_null),
            val_rev: Reverse(if is_null { 0 } else { val }),
            id_rev: Reverse(novel_id),
        }
    }
}

/// Output rank wrapper for numeric sorts (`BinaryHeap` keeps `limit+1` smallest items).
#[derive(Eq, PartialEq)]
pub struct NumericRankItem {
    pub descending: bool,
    pub key: CanonNumKey,
    pub doc: u32,
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
