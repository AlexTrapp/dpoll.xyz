import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.views import auth_logout
from django.http import Http404
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.utils.text import slugify
from django.utils.timezone import now
from steemconnect.client import Client
from steemconnect.operations import Comment

from .models import Question, Choice
from .post_templates import get_body
from .utils import get_sc_client


def index(request):
    polls = Question.objects.all().order_by("-id")[0:20]
    return render(request, "index.html", {"polls": polls})


def sc_login(request):

    if 'access_token' not in request.GET:

        login_url = get_sc_client().get_login_url(
            redirect_uri=settings.SC_REDIRECT_URI,
            scope="login,comment,comment_options",
        )
        return redirect(login_url)

    user = authenticate(access_token=request.GET.get("access_token"))

    if user is not None:
        if user.is_active:
            login(request, user)
            request.session["sc_token"] = request.GET.get("access_token")
            return redirect("/")
        else:
            return HttpResponse("Account is disabled.")
    else:
        return HttpResponse("Invalid login details.")


def sc_logout(request):
    auth_logout(request)
    return redirect("/")


def create_poll(request):
    if not request.user.is_authenticated:
        return redirect('login')

    error = False
    # @todo: create a form class for that. this is very ugly.
    if request.method == 'POST':

        if not 'sc_token' in request.session:
            return redirect("/")

        required_fields = ["question", "answers[]", "expire-at"]
        for field in required_fields:
            if not request.POST.get(field):
                error = True
                messages.add_message(
                    request,
                    messages.ERROR,
                    f"{field} field is required."
                )

        question = request.POST.get("question")
        choices = request.POST.getlist("answers[]")
        expire_at = request.POST.get("expire-at")

        if question:
            if not (4 < len(question) < 256):
                messages.add_message(
                    request,
                    messages.ERROR,
                    "Question text should be between 6-256 chars."
                )
                error = True

        if 'choices' in request.POST:
            if len(choices) < 2:
                messages.add_message(
                    request,
                    messages.ERROR,
                    f"At least 2 choices are required."
                )
                error = True

        if 'expire-at' in request.POST:
            if expire_at not in ["1_week", "1_month"]:
                messages.add_message(
                    request,
                    messages.ERROR,
                    f"Invalid expiration value."
                )
                error = True

        if error:
            return render(request, "add.html")

        days = 7 if expire_at == "1_week" else 30

        # add the question
        permlink = slugify(question)[0:256]

        # @todo: also check for duplicates in the blockchain.
        # client.get_content()
        if(Question.objects.filter(
                permlink=permlink, username=request.user)).exists():
            messages.add_message(
                request,
                messages.ERROR,
                "You have already a similar poll."
            )
            return redirect('create-poll')

        question = Question(
            text=question,
            username=request.user.username,
            permlink=permlink,
            expire_at=now() + timedelta(days=days)
        )
        question.save()

        # add answers attached to it
        for choice in choices:
            choice_instance = Choice(
                question=question,
                text=choice,
            )
            choice_instance.save()

        # send it to the steem blockchain
        sc_client = Client(access_token=request.session.get("sc_token"))
        comment = Comment(
            author=request.user.username,
            permlink=question.permlink,
            body=get_body(question, choices, request.user.username, permlink),
            title=question.text,
            parent_permlink=settings.COMMUNITY_TAG,
            json_metadata={
                "tags": settings.DEFAULT_TAGS,
                "app": "dpoll/0.0.1",
                "content_type": "poll",
            }
        )

        resp = sc_client.broadcast([comment.to_operation_structure()])
        if 'error' in resp:
            messages.add_message(
                request,
                messages.ERROR,
                resp.get("error_description", "error")
            )
            question.delete()
            return redirect('create-poll')

        return redirect('detail', question.username, question.permlink)

    return render(request, "add.html")


def detail(request, user, permlink):
    poll = Question.objects.get(username=user, permlink=permlink)
    choices = list(Choice.objects.filter(question=poll))
    all_votes = sum([c.votes for c in choices])
    choice_list = []
    for choice in choices:
        choice_data = choice
        if choice.votes:
            choice_data.percent = round(100 * choice.votes / all_votes, 2)
        else:
            choice_data.percent = 0
        choice_list.append(choice_data)

    user_vote = Choice.objects.filter(
        voted_users__username=request.user.username,
        question=poll,
    )
    if len(user_vote):
        user_vote = user_vote[0]
    else:
        user_vote = None

    return render(request, "poll_detail.html", {
        "poll": poll,
        "choices": choice_list,
        "total_votes": all_votes,
        "user_vote": user_vote,
    })


def vote(request, user, permlink):

    if request.method != "POST":
        raise Http404

    # django admin users should not be able to vote.
    if not request.session.get("sc_token"):
        redirect('logout')

    try:
        poll = Question.objects.get(username=user, permlink=permlink)
    except Question.DoesNotExist:
        raise Http404

    if not request.user.is_authenticated:
        return redirect('login')

    choice_id = request.POST.get("choice-id")

    if not choice_id:
        raise Http404

    if Choice.objects.filter(
            voted_users__username=request.user,
            question=poll).exists():
        messages.add_message(
            request,
            messages.ERROR,
            "You have already voted for this poll!"
        )
        return redirect("detail", poll.username, poll.permlink)

    if not poll.is_votable():
        messages.add_message(
            request,
            messages.ERROR,
                "This poll is expired!"
        )
        return redirect("detail", poll.username, poll.permlink)


    try:
        choice = Choice.objects.get(pk=int(choice_id))
    except Choice.DoesNotExist:
        raise Http404

    choice.voted_users.add(request.user)

    # send it to the steem blockchain
    sc_client = Client(access_token=request.session.get("sc_token"))
    comment = Comment(
        author=request.user.username,
        permlink=str(uuid.uuid4()),
        body=choice.text,
        parent_author=poll.username,
        parent_permlink=poll.permlink,
        json_metadata={
            "tags": settings.DEFAULT_TAGS,
            "app": "dpoll/0.0.1",
            "content_type": "poll_vote",
            "vote": choice.text,
        }
    )

    resp = sc_client.broadcast([comment.to_operation_structure()])
    if 'error' in resp:
        messages.add_message(
            request,
            messages.ERROR,
            resp.get("error_description", "error")
        )
        choice.voted_users.remove(request.user)

        return redirect("detail", poll.username, poll.permlink)

    messages.add_message(
        request,
        messages.SUCCESS,
        "You have sucessfully voted!"
    )

    return redirect("detail", poll.username, poll.permlink)
