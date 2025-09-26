from django.urls import path
from . import views


urlpatterns = [
    path("", views.library, name="library"),
    path("search/", views.search, name="search"),
    path("bookmarks/", views.bookmarks, name="bookmarks"),
    path("bookmark/<int:page_id>/toggle/", views.toggle_bookmark, name="toggle_bookmark"),
    path("compare/", views.compare, name="compare"),
    path("insights/", views.insights, name="insights"),
    path("tailor/", views.tailor, name="tailor"),
    path("<slug:slug>/page/<int:page_index>/", views.page_view, name="page"),
]


