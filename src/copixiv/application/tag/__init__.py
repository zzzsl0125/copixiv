"""Application layer — tag use cases."""

from copixiv.application.tag.list_preferences import ListPreferencesUseCase
from copixiv.application.tag.create_preference import CreatePreferenceUseCase
from copixiv.application.tag.update_preference import UpdatePreferenceUseCase
from copixiv.application.tag.delete_preference import DeletePreferenceUseCase
from copixiv.application.tag.reorder_preferences import ReorderPreferencesUseCase
from copixiv.application.tag.list_aliases import ListAliasesUseCase
from copixiv.application.tag.suggest_aliases import SuggestAliasesUseCase
from copixiv.application.tag.create_alias import CreateAliasUseCase
from copixiv.application.tag.delete_alias import DeleteAliasUseCase

__all__ = [
    "CreateAliasUseCase",
    "CreatePreferenceUseCase",
    "DeleteAliasUseCase",
    "DeletePreferenceUseCase",
    "ListAliasesUseCase",
    "ListPreferencesUseCase",
    "ReorderPreferencesUseCase",
    "SuggestAliasesUseCase",
    "UpdatePreferenceUseCase",
]
