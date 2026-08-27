/**
 * Frontend-bundled config defaults.
 *
 * The backend no longer serves ``default_min_like`` / ``default_min_text``
 * (the ``frontend`` config section was removed in the config cleanup); the
 * default filter values live here in the client instead.
 */

export const DEFAULT_MIN_LIKE = 500
export const DEFAULT_MIN_TEXT = 3000
