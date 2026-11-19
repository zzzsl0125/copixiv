mod arena;
mod engine;
mod filter;
mod hydrate;
mod interner;
mod model;
mod mutation;
mod postings;
mod query;
mod random;
mod staging;
mod storage;
mod tag_index;
mod text_index;
mod tracks;

use parking_lot::Mutex;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::engine::NovelIndex;
use crate::filter::{IntFilterField, QueryFilter, SortAxis, StrFilterField, TrackCursor};
use crate::model::{NovelRecord, QueryPage, TagCandidate, TagSuggestItem};
use crate::random::hex16_decode;
use crate::staging::StagingBuilder;

fn extract_rows(rows: &Bound<'_, PyAny>) -> PyResult<Vec<NovelRecord>> {
    let cap = rows.len().unwrap_or(0);
    let mut out = Vec::with_capacity(cap);
    for item in rows.try_iter()? {
        let obj = item?;
        let tup = obj.downcast_into::<PyTuple>()?;
        if tup.len() != 16 {
            return Err(PyValueError::new_err(format!(
                "Expected 16-tuple novel row, got len={}",
                tup.len()
            )));
        }
        out.push(NovelRecord {
            id: tup.get_borrowed_item(0)?.extract()?,
            title: tup.get_borrowed_item(1)?.extract()?,
            author_id: tup.get_borrowed_item(2)?.extract()?,
            author_name: tup.get_borrowed_item(3)?.extract()?,
            series_id: tup.get_borrowed_item(4)?.extract()?,
            series_name: tup.get_borrowed_item(5)?.extract()?,
            series_index: tup.get_borrowed_item(6)?.extract()?,
            text: tup.get_borrowed_item(7)?.extract()?,
            like: tup.get_borrowed_item(8)?.extract()?,
            view: tup.get_borrowed_item(9)?.extract()?,
            has_epub: tup.get_borrowed_item(10)?.extract()?,
            create_time_us: tup.get_borrowed_item(11)?.extract()?,
            source: tup.get_borrowed_item(12)?.extract()?,
            is_favourite: tup.get_borrowed_item(13)?.extract()?,
            is_special_follow: tup.get_borrowed_item(14)?.extract()?,
            tags: tup.get_borrowed_item(15)?.extract()?,
        });
    }
    Ok(out)
}

#[pyclass(name = "QueryFilter", frozen)]
pub struct PyQueryFilter {
    inner: QueryFilter,
}

#[pymethods]
impl PyQueryFilter {
    #[new]
    #[pyo3(signature = (
        tags = vec![],
        keyword_segments = vec![],
        int_filters = vec![],
        str_filters = vec![],
        only_favourite = false,
        only_special_follow = false,
        min_like = None,
        min_text = None,
        exclude_blocked_tags = None,
        count_blocked = false,
        restrict_ids = None,
        exclude_ids = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        tags: Vec<String>,
        keyword_segments: Vec<String>,
        int_filters: Vec<(String, i64)>,
        str_filters: Vec<(String, String)>,
        only_favourite: bool,
        only_special_follow: bool,
        min_like: Option<i64>,
        min_text: Option<i64>,
        exclude_blocked_tags: Option<bool>,
        count_blocked: bool,
        restrict_ids: Option<Vec<i64>>,
        exclude_ids: Option<Vec<i64>>,
    ) -> PyResult<Self> {
        let mut parsed_ints = Vec::with_capacity(int_filters.len());
        for (name, val) in int_filters {
            let field = IntFilterField::parse(&name).map_err(PyValueError::new_err)?;
            parsed_ints.push((field, val));
        }
        let mut parsed_strs = Vec::with_capacity(str_filters.len());
        for (name, val) in str_filters {
            let field = StrFilterField::parse(&name).map_err(PyValueError::new_err)?;
            parsed_strs.push((field, val));
        }
        Ok(Self {
            inner: QueryFilter {
                tags,
                keyword_segments,
                int_filters: parsed_ints,
                str_filters: parsed_strs,
                only_favourite,
                only_special_follow,
                min_like,
                min_text,
                exclude_blocked_tags,
                count_blocked,
                restrict_ids,
                exclude_ids,
            },
        })
    }
}

#[pyclass(name = "StagingEngine", frozen)]
pub struct PyStagingEngine {
    builder: Mutex<Option<StagingBuilder>>,
}

#[pymethods]
impl PyStagingEngine {
    #[new]
    #[pyo3(signature = (capacity = 0))]
    fn new(capacity: usize) -> Self {
        Self {
            builder: Mutex::new(Some(StagingBuilder::new(capacity))),
        }
    }

    fn push_batch(&self, py: Python<'_>, rows: &Bound<'_, PyAny>) -> PyResult<()> {
        let parsed = extract_rows(rows)?;
        py.allow_threads(|| {
            let mut guard = self.builder.lock();
            let builder = guard
                .as_mut()
                .ok_or_else(|| PyValueError::new_err("StagingEngine already finalized"))?;
            builder.push_batch(parsed);
            Ok(())
        })
    }
}

#[pyclass(name = "NovelQueryEngine", frozen)]
pub struct PyNovelQueryEngine {
    index: NovelIndex,
}

#[pymethods]
impl PyNovelQueryEngine {
    #[new]
    fn new() -> Self {
        Self {
            index: NovelIndex::new(),
        }
    }

    fn ready(&self) -> bool {
        self.index.ready()
    }

    fn needs_rebuild(&self) -> bool {
        self.index.needs_rebuild()
    }

    fn mark_needs_rebuild(&self) {
        self.index.mark_needs_rebuild();
    }

    fn stats(&self) -> (usize, usize, usize, bool, usize, bool) {
        self.index.stats()
    }

    fn memory_breakdown(&self) -> Vec<(String, usize, usize)> {
        self.index.memory_breakdown()
    }

    #[pyo3(signature = (
        staging,
        blocked_tags = vec![],
        exclude_blocked_default = true,
        special_follow_authors = vec![],
        tags = vec![],
        aliased_tag_ids = vec![],
    ))]
    #[allow(clippy::too_many_arguments)]
    fn swap_from_staging(
        &self,
        py: Python<'_>,
        staging: &PyStagingEngine,
        blocked_tags: Vec<String>,
        exclude_blocked_default: bool,
        special_follow_authors: Vec<i64>,
        tags: Vec<(i64, String, i32)>,
        aliased_tag_ids: Vec<i64>,
    ) -> PyResult<()> {
        let builder = staging
            .builder
            .lock()
            .take()
            .ok_or_else(|| PyValueError::new_err("StagingEngine already finalized"))?;
        py.allow_threads(|| {
            let state = builder.finalize(
                blocked_tags,
                exclude_blocked_default,
                special_follow_authors,
                tags,
                aliased_tag_ids,
            );
            self.index.swap_state(state);
        });
        Ok(())
    }

    fn upsert_batch(&self, py: Python<'_>, rows: &Bound<'_, PyAny>) -> PyResult<()> {
        let parsed = extract_rows(rows)?;
        py.allow_threads(|| {
            self.index.upsert_batch(parsed);
        });
        Ok(())
    }

    fn delete_ids(&self, py: Python<'_>, novel_ids: Vec<i64>) {
        py.allow_threads(|| {
            self.index.delete_ids(&novel_ids);
        });
    }

    fn toggle_special_follow(&self, py: Python<'_>, author_id: i64, is_special_follow: bool) {
        py.allow_threads(|| {
            self.index
                .toggle_special_follow(author_id, is_special_follow);
        });
    }

    fn set_blocked_tags(&self, py: Python<'_>, blocked_tags: Vec<String>) {
        py.allow_threads(|| {
            self.index.set_blocked_tags(blocked_tags);
        });
    }

    fn set_exclude_blocked_default(&self, py: Python<'_>, value: bool) {
        py.allow_threads(|| {
            self.index.set_exclude_blocked_default(value);
        });
    }

    #[pyo3(signature = (
        delete_ids = vec![],
        upsert_rows = None,
        blocked_tags = None,
        exclude_blocked_default = None,
        special_follow_changes = vec![],
        replace_all_tags = None,
        delete_tag_names = None,
        upsert_tags = None,
        aliased_tag_ids = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn apply_delta(
        &self,
        py: Python<'_>,
        delete_ids: Vec<i64>,
        upsert_rows: Option<&Bound<'_, PyAny>>,
        blocked_tags: Option<Vec<String>>,
        exclude_blocked_default: Option<bool>,
        special_follow_changes: Vec<(i64, bool)>,
        replace_all_tags: Option<Vec<(i64, String, i32)>>,
        delete_tag_names: Option<Vec<String>>,
        upsert_tags: Option<Vec<(i64, String, i32)>>,
        aliased_tag_ids: Option<Vec<i64>>,
    ) -> PyResult<()> {
        let parsed_rows = match upsert_rows {
            Some(rows) => extract_rows(rows)?,
            None => Vec::new(),
        };
        py.allow_threads(|| {
            self.index.apply_delta(
                &delete_ids,
                parsed_rows,
                blocked_tags,
                exclude_blocked_default,
                &special_follow_changes,
                replace_all_tags,
                delete_tag_names,
                upsert_tags,
                aliased_tag_ids,
            );
        });
        Ok(())
    }

    #[pyo3(signature = (
        filter,
        order_by = "id",
        descending = true,
        limit = 20,
        cursor_int = None,
        cursor_str = None,
        cursor_id = None,
        random_seed_hex = None,
        random_h_hex = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn query_novel_rows(
        &self,
        py: Python<'_>,
        filter: &PyQueryFilter,
        order_by: &str,
        descending: bool,
        limit: usize,
        cursor_int: Option<i64>,
        cursor_str: Option<String>,
        cursor_id: Option<i64>,
        random_seed_hex: Option<String>,
        random_h_hex: Option<String>,
    ) -> PyResult<QueryPage<NovelRecord>> {
        let axis = SortAxis::parse(order_by).map_err(PyValueError::new_err)?;
        let cursor = build_cursor(
            axis,
            cursor_int,
            cursor_str,
            cursor_id,
            random_seed_hex,
            random_h_hex,
        )
        .map_err(PyValueError::new_err)?;
        let filter_ref = &filter.inner;
        py.allow_threads(|| {
            self.index
                .query_novel_rows(filter_ref, axis, descending, limit, cursor.as_ref())
        })
        .map_err(PyValueError::new_err)
    }

    fn get_novel_rows_by_ids(&self, py: Python<'_>, novel_ids: Vec<i64>) -> Vec<NovelRecord> {
        py.allow_threads(|| self.index.get_novel_rows_by_ids(&novel_ids))
    }

    fn count_novels(&self, py: Python<'_>, filter: &PyQueryFilter, cap: usize) -> (usize, bool) {
        let filter_ref = &filter.inner;
        py.allow_threads(|| self.index.count_novels(filter_ref, cap))
    }

    fn has_match(&self, py: Python<'_>, filter: &PyQueryFilter) -> bool {
        let filter_ref = &filter.inner;
        py.allow_threads(|| self.index.has_match(filter_ref))
    }

    fn list_matching_ids(&self, py: Python<'_>, filter: &PyQueryFilter) -> Vec<i64> {
        let filter_ref = &filter.inner;
        py.allow_threads(|| self.index.list_matching_ids(filter_ref))
    }

    fn list_blocked_ids(&self, py: Python<'_>) -> Vec<i64> {
        py.allow_threads(|| self.index.list_blocked_ids())
    }

    #[pyo3(signature = (novel_ids, order_by = "like", descending = true))]
    fn sort_ids(
        &self,
        py: Python<'_>,
        novel_ids: Vec<i64>,
        order_by: &str,
        descending: bool,
    ) -> PyResult<Vec<i64>> {
        let axis = SortAxis::parse(order_by).map_err(PyValueError::new_err)?;
        Ok(py.allow_threads(|| self.index.sort_ids(&novel_ids, axis, descending)))
    }

    #[pyo3(signature = (limit = 5, offset = 0, target_tag = None))]
    fn suggest_tag_aliases(
        &self,
        py: Python<'_>,
        limit: usize,
        offset: i64,
        target_tag: Option<String>,
    ) -> (Vec<TagSuggestItem>, i64) {
        py.allow_threads(|| {
            self.index
                .suggest_tag_aliases(limit, offset, target_tag.as_deref())
        })
    }
}

fn build_cursor(
    axis: SortAxis,
    cursor_int: Option<i64>,
    cursor_str: Option<String>,
    cursor_id: Option<i64>,
    random_seed_hex: Option<String>,
    random_h_hex: Option<String>,
) -> Result<Option<TrackCursor>, String> {
    let Some(cid) = cursor_id else {
        return Ok(None);
    };
    match axis {
        SortAxis::Numeric(crate::filter::NumericField::AbsId) => {
            Ok(Some(TrackCursor::Int(cid, cid)))
        }
        SortAxis::Numeric(_) => {
            let val = cursor_int.ok_or_else(|| "Numeric cursor requires cursor_int".to_string())?;
            Ok(Some(TrackCursor::Int(val, cid)))
        }
        SortAxis::String(_) => {
            let val = cursor_str.ok_or_else(|| "String cursor requires cursor_str".to_string())?;
            Ok(Some(TrackCursor::Str(val, cid)))
        }
        SortAxis::Random => {
            let sh = random_seed_hex
                .as_deref()
                .ok_or_else(|| "Random cursor requires random_seed_hex".to_string())?;
            let hh = random_h_hex
                .as_deref()
                .ok_or_else(|| "Random cursor requires random_h_hex".to_string())?;
            Ok(Some(TrackCursor::Random(
                hex16_decode(sh)?,
                hex16_decode(hh)?,
                cid,
            )))
        }
        SortAxis::None => Err("Cannot use cursor with order_by=none".to_string()),
    }
}

#[pymodule]
fn copixiv_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyQueryFilter>()?;
    m.add_class::<PyStagingEngine>()?;
    m.add_class::<PyNovelQueryEngine>()?;
    m.add_class::<NovelRecord>()?;
    m.add_class::<TagCandidate>()?;
    m.add_class::<TagSuggestItem>()?;
    Ok(())
}
