import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_not_required, login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from app import helpers
from app.models import Item, MediaManager, MediaTypes
from app.providers import services
from lists.forms import CustomListForm
from lists.models import CustomList, CustomListItem
from users.models import ListDetailSortChoices, ListSortChoices, MediaStatusChoices

logger = logging.getLogger(__name__)
User = get_user_model()


def _prefetch_custom_lists(queryset):
    """Apply standard prefetching for custom list querysets."""
    return queryset.select_related("owner").prefetch_related(
        "collaborators",
        Prefetch(
            "items",
            queryset=Item.objects.order_by("-customlistitem__date_added"),
        ),
        Prefetch(
            "customlistitem_set",
            queryset=CustomListItem.objects.order_by("-date_added"),
        ),
    )


def _get_custom_lists(request, target_user):
    """Return (queryset, sort_by, is_owner) for the lists page."""
    is_owner = request.user.is_authenticated and request.user == target_user
    sort_param = request.GET.get("sort")

    if is_owner:
        return (
            CustomList.objects.get_user_lists(target_user),
            request.user.update_preference("lists_sort", sort_param),
            True,
        )

    if not target_user.is_public:
        msg = "User not found"
        raise Http404(msg)

    return (
        _prefetch_custom_lists(
            CustomList.objects.filter(owner=target_user, is_public=True),
        ),
        target_user.get_valid_preference("lists_sort", sort_param),
        False,
    )


def _apply_lists_search(custom_lists, search_query):
    """Apply search filters for lists."""
    if not search_query:
        return custom_lists

    return custom_lists.filter(
        Q(name__icontains=search_query) | Q(description__icontains=search_query),
    )


def _apply_lists_sort(custom_lists, sort_by):
    """Apply sorting for lists."""
    if sort_by == "name":
        return custom_lists.order_by("name")

    if sort_by == "items_count":
        return custom_lists.annotate(
            items_count=Count("items", distinct=True),
        ).order_by("-items_count")

    if sort_by == "newest_first":
        return custom_lists.order_by("-id")

    # last_item_added is the default
    return custom_lists.annotate(
        latest_update=Subquery(
            CustomListItem.objects.filter(
                custom_list=OuterRef("pk"),
            )
            .order_by("-date_added")
            .values("date_added")[:1],
        ),
    ).order_by("-latest_update", "name")


def _attach_custom_list_forms(lists_page):
    """Attach a CustomListForm instance to each list card (select2 needs stable ids)."""
    for i, custom_list in enumerate(lists_page, start=1):
        custom_list.form = CustomListForm(
            instance=custom_list,
            auto_id=f"id_{i}_%s",
        )


def _build_lists_context(lists_page, target_user, sort_by):
    return {
        "custom_lists": lists_page,
        "target_user": target_user,
        "current_sort": sort_by,
        "sort_choices": ListSortChoices.choices,
    }


@login_not_required
@require_GET
def lists(request, username):
    """Return the custom list page."""
    target_user = get_object_or_404(User, username=username)
    custom_lists, sort_by, is_owner = _get_custom_lists(request, target_user)

    custom_lists = _apply_lists_search(custom_lists, request.GET.get("q", ""))
    custom_lists = _apply_lists_sort(custom_lists, sort_by)

    page = request.GET.get("page", 1)
    paginator = Paginator(custom_lists, 20)
    lists_page = paginator.get_page(page)

    context = _build_lists_context(lists_page, target_user, sort_by)
    if is_owner:
        _attach_custom_list_forms(lists_page)
        context["form"] = CustomListForm()

    if request.headers.get("HX-Request"):
        return render(request, "lists/components/list_grid.html", context)

    return render(request, "lists/custom_lists.html", context)


@login_not_required
@require_GET
def list_detail(request, username, list_id):
    """Return the detail page of a custom list."""
    target_user = get_object_or_404(User, username=username)
    custom_list = get_object_or_404(
        CustomList.objects.select_related("owner").prefetch_related("collaborators"),
        id=list_id,
        owner=target_user,
    )

    is_owner = request.user.is_authenticated and request.user == target_user
    can_view = custom_list.user_can_view(request.user) or custom_list.is_public
    if not can_view:
        msg = "List not found"
        raise Http404(msg)

    media_user = target_user
    if is_owner:
        sort_by = request.user.update_preference(
            "list_detail_sort",
            request.GET.get("sort"),
        )
        status_filter = request.user.update_preference(
            "list_detail_status",
            request.GET.get("status"),
        )
    else:
        sort_by = target_user.get_valid_preference(
            "list_detail_sort",
            request.GET.get("sort"),
        )
        status_filter = target_user.get_valid_preference(
            "list_detail_status",
            request.GET.get("status"),
        )

    params = {
        "sort_by": sort_by,
        "media_type": request.GET.get("type", "all"),
        "status_filter": status_filter,
        "page": int(request.GET.get("page", 1)),
        "search_query": request.GET.get("q", ""),
    }

    items = custom_list.items.all()
    if params["search_query"]:
        items = items.filter(title__icontains=params["search_query"])
    if params["media_type"] != "all":
        items = items.filter(media_type=params["media_type"])

    media_types = items.values_list("media_type", flat=True).distinct()
    media_manager = MediaManager()
    media_by_item_id = {}

    if params["status_filter"] != MediaStatusChoices.ALL:
        item_ids = items.values_list("id", flat=True)
        media_by_item_id = media_manager.fetch_media_for_items(
            media_types,
            item_ids,
            media_user,
            status_filter=params["status_filter"],
        )
        items = items.filter(id__in=media_by_item_id.keys())

    sort_mapping = {
        "date_added": ["-customlistitem__date_added"],
        "title": [
            F("title").asc(nulls_last=True),
            F("season_number").asc(nulls_first=True),
            F("episode_number").asc(nulls_first=True),
        ],
        "media_type": ["media_type"],
    }
    items = items.order_by(
        *sort_mapping.get(params["sort_by"], ["-customlistitem__date_added"]),
    )

    paginator = Paginator(items, 16)
    items_page = paginator.get_page(params["page"])

    if params["status_filter"] == MediaStatusChoices.ALL:
        media_types_in_page = {item.media_type for item in items_page}
        page_item_ids = [item.id for item in items_page]
        media_by_item_id = media_manager.fetch_media_for_items(
            media_types_in_page,
            page_item_ids,
            media_user,
        )

    for item in items_page:
        item.media = media_by_item_id.get(item.id)

    context = {
        "custom_list": custom_list,
        "target_user": target_user,
        "items": items_page,
        "has_next": items_page.has_next(),
        "next_page_number": items_page.next_page_number()
        if items_page.has_next()
        else None,
        "current_sort": params["sort_by"],
        "current_status": params["status_filter"] or MediaStatusChoices.ALL,
        "sort_choices": ListDetailSortChoices.choices,
        "status_choices": MediaStatusChoices.choices,
    }

    if not request.headers.get("HX-Request"):
        if custom_list.user_can_edit(request.user):
            context["form"] = CustomListForm(instance=custom_list)
        context.update(
            {
                "media_types": MediaTypes.values,
                "items_count": paginator.count,
                "collaborators_count": custom_list.collaborators.count() + 1,
            },
        )
        return render(request, "lists/list_detail.html", context)

    return render(request, "lists/components/media_grid.html", context)


@login_required
@require_POST
def create(request):
    """Create a new custom list."""
    form = CustomListForm(request.POST)
    if form.is_valid():
        custom_list = form.save(commit=False)
        custom_list.owner = request.user
        custom_list.save()
        form.save_m2m()
        logger.info("%s list created successfully.", custom_list)
    else:
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)
    return helpers.redirect_back(request)


@login_required
@require_POST
def edit(request):
    """Edit an existing custom list."""
    list_id = request.POST.get("list_id")
    custom_list = get_object_or_404(CustomList, id=list_id)
    if custom_list.user_can_edit(request.user):
        form = CustomListForm(request.POST, instance=custom_list)
        if form.is_valid():
            form.save()
            logger.info("%s list edited successfully.", custom_list)
    else:
        messages.error(request, "You do not have permission to edit this list.")
    return helpers.redirect_back(request)


@login_required
@require_POST
def delete(request):
    """Delete a custom list."""
    list_id = request.POST.get("list_id")
    custom_list = get_object_or_404(CustomList, id=list_id)
    if custom_list.user_can_delete(request.user):
        custom_list.delete()
        logger.info("%s list deleted successfully.", custom_list)
        return redirect("lists", username=request.user.username)

    messages.error(request, "You do not have permission to delete this list.")
    return helpers.redirect_back(request)


@require_GET
def lists_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the modal showing all custom lists and allowing to add to them."""
    try:
        item = Item.objects.get(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
        )
    except Item.DoesNotExist:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
            episode_number,
        )
        item = Item.objects.create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
            title=metadata["title"],
            image=metadata["image"],
        )

    custom_lists = CustomList.objects.get_user_lists_with_item(request.user, item)

    return render(
        request,
        "lists/components/fill_lists.html",
        {"item": item, "custom_lists": custom_lists},
    )


@login_required
@require_POST
def list_item_toggle(request):
    """Add or remove an item from a custom list."""
    item_id = request.POST["item_id"]
    custom_list_id = request.POST["custom_list_id"]

    item = get_object_or_404(Item, id=item_id)
    custom_list = get_object_or_404(
        CustomList.objects.filter(
            Q(owner=request.user) | Q(collaborators=request.user),
            id=custom_list_id,
        ).distinct(),
    )

    if custom_list.items.filter(id=item.id).exists():
        custom_list.items.remove(item)
        logger.info("%s removed from %s.", item, custom_list)
        has_item = False
    else:
        custom_list.items.add(item)
        logger.info("%s added to %s.", item, custom_list)
        has_item = True

    return render(
        request,
        "lists/components/list_item_button.html",
        {"custom_list": custom_list, "item": item, "has_item": has_item},
    )
