use pyo3::prelude::*;

pub type QueryPage<T> = (Vec<T>, bool, Option<(String, String, i64)>);

/// Canonical row projected from PostgreSQL (slim columns, without heavy `content`).
/// Directly exposed as `copixiv_rs.NovelRecord` to eliminate duplicate Rust/PyO3 DTOs.
#[pyclass(name = "NovelRecord", frozen, get_all)]
#[derive(Clone, Debug)]
pub struct NovelRecord {
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

/// Tag suggestion candidate returned by `NovelQueryEngine.suggest_tag_aliases`.
#[pyclass(name = "TagCandidate", frozen, get_all)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TagCandidate {
    pub id: i64,
    pub name: String,
    pub reference_count: i32,
}

/// Grouped alias suggestion item: one target tag and its top candidates.
#[pyclass(name = "TagSuggestItem", frozen, get_all)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TagSuggestItem {
    pub target: TagCandidate,
    pub candidates: Vec<TagCandidate>,
}
