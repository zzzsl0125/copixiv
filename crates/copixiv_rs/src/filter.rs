#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NumericField {
    AbsId = 0,
    CreateTime = 1,
    Like = 2,
    View = 3,
    Text = 4,
    AuthorId = 5,
    SeriesId = 6,
    SeriesIndex = 7,
    HasEpub = 8,
}

impl NumericField {
    pub const COUNT: usize = 9;

    pub const ALL: [NumericField; Self::COUNT] = [
        Self::AbsId,
        Self::CreateTime,
        Self::Like,
        Self::View,
        Self::Text,
        Self::AuthorId,
        Self::SeriesId,
        Self::SeriesIndex,
        Self::HasEpub,
    ];

    pub const HOT: [NumericField; 5] = [
        Self::AbsId,
        Self::CreateTime,
        Self::Like,
        Self::View,
        Self::Text,
    ];

    #[inline]
    pub const fn bit(self) -> u16 {
        1u16 << (self as usize)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StringSortField {
    Title,
    AuthorName,
    SeriesName,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SortAxis {
    Numeric(NumericField),
    String(StringSortField),
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
            "author_id" => Ok(Self::Numeric(NumericField::AuthorId)),
            "series_id" => Ok(Self::Numeric(NumericField::SeriesId)),
            "series_index" => Ok(Self::Numeric(NumericField::SeriesIndex)),
            "has_epub" => Ok(Self::Numeric(NumericField::HasEpub)),
            "title" => Ok(Self::String(StringSortField::Title)),
            "author_name" => Ok(Self::String(StringSortField::AuthorName)),
            "series_name" => Ok(Self::String(StringSortField::SeriesName)),
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
    Int(i64, i64),
    Str(String, i64),
    Random(u64, u64, i64), // (seed, h, id)
}
