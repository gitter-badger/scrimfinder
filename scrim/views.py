from scrim import scrim_app, oid, db, models, lm
from models import User, Team, Request, Membership, Comment
from utils import utils
from flask import redirect, session, g, json, render_template, flash, url_for
from flask.ext.login import login_user, logout_user, current_user, login_required
import requests
import re
from sqlalchemy import func, and_
from forms import UserEditForm, CreateTeamForm, TeamEditForm, FilterTeamForm, FilterScrimForm, TeamCommentForm, ProposeScrimForm
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound
from sqlalchemy.exc import OperationalError

#========================
# Helper function
#========================

def map_days(days):
        # Map a list of words to the "bit string"
        # Maybe there's a shorter and cleverer way to do it
        # but we wont show it to Tristan so that's fine
        words = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday", "Sunday"]
        aval = [(word if int(day)==1 else False) 
                for day,word in zip(days,words)]
        return aval

# Steam Web APIs...

def get_steam_user_info(steam_id):
    get_player_summaries_api = \
    'http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?'

    params = {
        'key': scrim_app.config['STEAM_API_KEY'],
        'steamids': steam_id,
        'format': json
    }

    response = requests.get(url=get_player_summaries_api, params=params)
    user_info = response.json()

    return user_info['response']['players'][0] or {}

def get_recently_played_games(steam_id):
    get_recently_played_games_api = \
    'http://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v0001/?'

    params = {
        'key': scrim_app.config['STEAM_API_KEY'],
        'steamid': steam_id, 
        'format': json
    }

    response = requests.get(url=get_recently_played_games_api, params=params)
    recently_played_games = response.json()

    return recently_played_games['response'] or {}

@scrim_app.route('/')
@scrim_app.route('/index')
def index():
    """
    Home page. empty for now.
    """

    nname = None
    if g.user is not None:
        nname = g.user.nickname

    return render_template('index.html',user=nname)

@scrim_app.route('/login')
@oid.loginhandler
def login():
    """
    Logs in using steam.
    """

    if g.user is not None:
        return redirect(oid.get_next_url())
    else:
        return oid.try_login('http://steamcommunity.com/openid')

@oid.after_login
def after_login(resp):
    """
    Called after successful log in.
    Creates a new user or gets the existing one
    """

    steam_id_regex = re.compile('steamcommunity.com/openid/id/(.*?)$')
    steam_id = steam_id_regex.search(resp.identity_url).group(1)

    from datetime import datetime as dt

    try:
        g.user = User.query.filter_by(steam_id=steam_id).one()
    except NoResultFound:
        g.user = User()
        steam_data = get_steam_user_info(steam_id)
        g.user.steam_id     = steam_id
        g.user.nickname     = steam_data['personaname']
        g.user.profile_url  = steam_data['profileurl']
        g.user.avatar_url   = steam_data['avatar']
        g.user.join_date    = dt.utcnow()
        g.user.last_online  = dt.utcnow()
        db.session.add(g.user)
    
    last_online = g.user.last_online
    # do something

    g.user.last_online = dt.utcnow()
    db.session.commit()
    login_user(g.user)
    return redirect(oid.get_next_url())

@scrim_app.before_request
def before_request():
    """
    This gets called before each request and checks the session.
    Will probably do more stuff.
    """

    g.user = None
    if 'user_id' in session:
        g.user = User.query.filter_by(id=session['user_id']).first()

@scrim_app.route('/logout')
def logout():
    logout_user()
    flash("Logged out")
    return redirect(url_for('index'))

@scrim_app.route('/user/<steam_id>')
def user_page(steam_id):
    try:
        user = User.query.filter_by(steam_id=steam_id).one()
    except NoResultFound:
        flash('User not found')
        return redirect(url_for('index'))

    # array of tuples of Team and role e.g. [(team,"Captain"),(team,"Coach")]
    teams_roles = []
    memberships = Membership.query.filter_by(user_id=user.id).all()
    for mem in memberships:
        team = Team.query.filter_by(id=mem.team_id).one()
        teams_roles.append((team, mem.role))

    return render_template('user.html',
            id=user.steam_id,
            nick=user.nickname,
            profile_url=user.profile_url,
            avatar=user.avatar_url,
            teams_roles=teams_roles)

@scrim_app.route('/users')
@scrim_app.route('/users/page/<int:page>')
def show_all_users(page=1):
    """
    Retrieve all users of the application, 50 results per page
    """

    if page < 1:
        abort(404)

    from config import USERS_PER_PAGE
    try:
        users_list = User.query.paginate(page, per_page=USERS_PER_PAGE)
    except OperationalError: # no user in db
        users_list = None
    
    return render_template('all_users.html', users_list=users_list)

@scrim_app.route('/teams', methods=['GET','POST'])
@scrim_app.route('/teams/page/<int:page>', methods=['GET','POST'])
def show_all_teams(page=1):
    if page < 1:
        abort(404)

    query = Team.query
    form = FilterTeamForm()
    if form.validate_on_submit():
        if form.team_name != "":
            query = query.filter(Team.name.like('%'+form.team_name.data+'%'))
        if form.team_skill_level.data != "ALL":
            query = query.filter_by(skill_level=form.team_skill_level.data)
        if form.team_time_zone.data != "ALL":
            query = query.filter_by(time_zone=form.team_time_zone.data)

    from config import TEAMS_PER_PAGE
    try:
        teams_list = query.paginate(page, per_page=TEAMS_PER_PAGE)
    except OperationalError: # no team in db
        teams_list = None
    
    return render_template('all_teams.html', teams_list=teams_list, form=form)

@scrim_app.route('/scrims', methods=['GET','POST'])
@scrim_app.route('/scrims/page/<int:page>', methods=['GET','POST'])
@login_required
def show_all_scrims(page=1):
    """
    Scrim search page. Only visible for users who are part of a team
    """

    if page < 1:
        abort(404)

    form = FilterScrimForm()

    user_membership = Membership.query.filter_by(user_id=g.user.id).all()
    if len(user_membership) == 0:
        flash('You are not in a team. Cannot search for scrims.')
        return render_template('all_scrims.html', teams_list=None, form=form)

    query = Team.query
    for membership in user_membership:
            query = query.filter(Team.id != membership.team_id)
    if form.validate_on_submit():
        if form.team_skill_level.data != "ALL":
            query = query.filter_by(skill_level=form.team_skill_level.data)
        if form.team_time_zone.data != "ALL":
            query = query.filter_by(time_zone=form.team_time_zone.data)
        
        week = list("0000000")
        week[0] = str(int(form.mon.data))
        week[1] = str(int(form.tue.data))
        week[2] = str(int(form.wed.data))
        week[3] = str(int(form.thu.data))
        week[4] = str(int(form.fri.data))
        week[5] = str(int(form.sat.data))
        week[6] = str(int(form.sun.data))
        week = "".join(week)
        possible_weekdays = utils.get_bit_combinations(week)
        query = query.filter(Team.week_days.in_(possible_weekdays))
    else:
        # set default values HACK - use user's first team
        first_team = Team.query.filter_by(id=user_membership[0].team_id).one()

        query = query.filter_by(skill_level=first_team.skill_level)
        query = query.filter_by(time_zone=first_team.time_zone)
        
        # continue - set scrim time preferences
        first_team_weekdays = first_team.week_days
        possible_weekdays = utils.get_bit_combinations(first_team_weekdays)
        query = query.filter(Team.week_days.in_(possible_weekdays))

    from config import TEAMS_PER_PAGE
    try:
        teams_list = query.paginate(page, per_page=TEAMS_PER_PAGE)
    except OperationalError: # no team in db
        teams_list = None

    return render_template('all_scrims.html', teams_list=teams_list, form=form)

@lm.user_loader
def load_user(id):
    return User.query.get(int(id))

@scrim_app.route('/edit_profile', methods = ['GET', 'POST'])
@login_required
def edit_profile():
    """
    This can be done once the user model is finalized
    so that constat modifications are avoided
    """
    form = EditForm()
    if form.validate_on_submit():
        #g.user.team_name = form.team_name.data
        #g.user.team_skill_level = form.team_skill_level.data
        #g.user.team_time_zone = form.team_time_zone.data
        #db.session.add(g.user)
        #db.session.commit()
        #flash('Your changes have been saved')
        return redirect(url_for('user_page', steam_id=g.user.steam_id))
    else:
        #form.team_name.data = g.user.team_name
        #form.team_skill_level.data = g.user.team_skill_level
        #form.team_time_zone.data = g.user.team_time_zone
        #times.day.data = aval.day
        #times.time_from.data = aval.time_from
        #times.time_to.data = aval.time_to
        return render_template('edit_profile.html', form = form)

@scrim_app.route('/edit_team/<int:team_id>', methods = ['GET', 'POST'])
@login_required
def edit_team(team_id):
    """

    """
    has_right = False

    # Need to check if the user has the right to edit this team
    # Search through his memberships
    #teams = Team.query.join(Membership).filter_by(user_id=g.user.id).all()
    teams = Membership.query.filter_by(user_id=g.user.id).all()
    for team in teams:
        if team.team_id == int(team_id) and team.role == "Captain":
            has_right = True
    
    # Kick him out if not
    if not has_right:
        flash("You should not be here")
        return redirect(url_for('index'))

    # Get the team the is being edited
    try:
        team_edit = Team.query.filter_by(id=team_id).one()
    except NoResultFound, e:
        flash("Team not found")
        return redirect(url_for('index'))

    form = TeamEditForm()
    if form.validate_on_submit():
        team_edit.name = form.team_name.data
        team_edit.skill_level = form.team_skill_level.data
        team_edit.time_zone = form.team_time_zone.data

        # Make week string
        week = list("0000000")
        week[0] = str(int(form.mon.data))
        week[1] = str(int(form.tue.data))
        week[2] = str(int(form.wed.data))
        week[3] = str(int(form.thu.data))
        week[4] = str(int(form.fri.data))
        week[5] = str(int(form.sat.data))
        week[6] = str(int(form.sun.data))
        week = "".join(week)

        team_edit.time_from = form.time_from.data
        team_edit.week_days = week
        db.session.add(team_edit)
        db.session.commit()
        return redirect(url_for('team_page', team_id=team_id))
    else:
        wk = team_edit.week_days
        print wk
        form.team_name.data = team_edit.name
        form.team_skill_level.data = team_edit.skill_level
        form.team_time_zone.data = team_edit.time_zone
        form.time_from.data = team_edit.time_from
        form.mon.data = bool(int(wk[0]))
        form.tue.data = bool(int(wk[1]))
        form.wed.data = bool(int(wk[2]))
        form.thu.data = bool(int(wk[3]))
        form.fri.data = bool(int(wk[4]))
        form.sat.data = bool(int(wk[5]))
        form.sun.data = bool(int(wk[6]))
        return render_template('edit_team.html', form=form, team_id=team_id)

@scrim_app.route('/create_team', methods = ['GET', 'POST'])
@login_required
def create_team():
    """
    A user creates a new team and sets some team parameters
    The suer by default becomes the team leader
    """

    form = CreateTeamForm()

    user_membership = Membership.query.filter_by(user_id=g.user.id).all()
    if len(user_membership) == 3:
        flash('You are in three teams already. Chill.')
        return redirect(url_for('user_page', steam_id=g.user.steam_id))

    if form.validate_on_submit():
        new_team = Team()
        new_team.name = form.team_name.data
        new_team.skill_level = form.team_skill_level.data
        new_team.time_zone = form.team_time_zone.data

        week = list("0000000")
        week[0] = str(int(form.mon.data))
        week[1] = str(int(form.tue.data))
        week[2] = str(int(form.wed.data))
        week[3] = str(int(form.thu.data))
        week[4] = str(int(form.fri.data))
        week[5] = str(int(form.sat.data))
        week[6] = str(int(form.sun.data))
        week = "".join(week)
        new_team.week_days = week

        db.session.add(new_team)
        db.session.commit()
        mem = Membership()
        mem.team_id = new_team.id
        mem.user_id = g.user.id
        # Charlito: sounds better
        mem.role = "Captain" 
        db.session.add(mem)
        db.session.commit()
        return redirect(url_for('user_page', steam_id=g.user.steam_id))
    else:
        return render_template('create_team.html', create_team_form=form)

@scrim_app.route('/quit_team/<int:team_id>', methods=['POST'])
@login_required
def quit_team(team_id):
    if team_id < 1:
        abort(404)

    team_membership = None
    user_membership = Membership.query.filter_by(user_id=g.user.id).all()
    for membership in user_membership:
        if membership.team_id == team_id:
            team_membership = membership

    if team_membership is None:
        flash("Dude you are not in this team")
        redirect(url_for('user_page'), steam_id=g.user.steam_id)
    else:
        db.session.delete(team_membership)
        db.session.commit()

        user_team = Team.query.filter_by(id=team_id).one()
        user_team_name = user_team.name
        members = Team.query.join(Membership).filter_by(team_id=team_id).all()
        if len(members) == 0:
            db.session.delete(user_team)
            db.session.commit()
            flash("You quit from team " + user_team_name + " , and since \
                    there's no one left in the team, the team is deleted")
        else:
            flash("You quit from team " + user_team_name)
        return redirect(url_for('user_page', steam_id=g.user.steam_id))

@scrim_app.route('/team/<team_id>', methods=['GET','POST'])
def team_page(team_id):
    """
    Currently shows a specific team, with team parameters, current members
    and pending members

    The pending members view will be restricted to the team leader
    """
    
    form = TeamCommentForm()

    try:
        team = Team.query.filter_by(id=team_id).one()
    except NoResultFound as e:
        flash("Team not found")
        return redirect(url_for('index'))
#<<<<<<< HEAD
    

    all_members = Membership.query.join(Team).filter_by(id=team_id).all()
    # an arry of tuples of User and role e.g. [(user,"Captain"),(user,"BenchPlayer")]
    members_roles = []
    for mem in all_members:
        user = User.query.filter_by(id=mem.user_id).one()
        members_roles.append((user, mem.role))

    #
    #   This can be optimized by first filtering then joining
    #
    pendings = User.query.join(Request).filter(User.id==Request.user_id) \
                .filter(Request.team_id==team_id).all()
    
    # Is the user in the team
    in_team = False
    if g.user in (x[0] for x in members_roles):
            in_team = True

    # propose scrim if and only if the user is not in the team & is a captain
    # of some team
    is_captain = False
    if g.user is not None:
        user_memberships = Membership.query.filter_by(user_id=g.user.id)
        for mem in user_memberships:
            if mem.role == 'Captain':
                is_captain = True
                break

    propose_scrim = False
    if in_team == False and is_captain:
        propose_scrim = True


    # What's the team availability in days
    days = team.week_days
    aval = map_days(days)

    
    if form.validate_on_submit():
        com = Comment()
        com.team_id = team_id
        com.user_id = g.user.id
        com.comment = form.text.data

        db.session.add(com)
        db.session.commit()
        return redirect(url_for("team_page", team_id=team_id))
    else:
        comment_list =[]
        try:
            cmnts = Comment.query.join(User).all()
            for x in cmnts:
                comment_list.append((x.user.nickname,x.comment))

        except NoResultFound, e:
            comment_list = False

        if g.user == None:
            dont_show = True
        else:
            dont_show = False

        return render_template('team.html',
                team_id=team.id,
                team_name=team.name,
                team_skill=team.skill_level,
                team_zone=team.time_zone,
                team_time=team.time_from,
                members_roles=members_roles,
                pendings=pendings,
                in_team=in_team,
                aval=aval,
                form=form,
                com_list=comment_list,
                dont_show=dont_show,
                propose_scrim=propose_scrim,
                team=team)

@scrim_app.route('/team/join/<team_id>')
@login_required
def team_join(team_id):
    """
    Makes a new request to join a certain team
    """

    try:
        user = User.query.filter_by(id=g.user.id).one()
    except NoResultFound, e:
        flash("User not found")
        return redirect(url_for('index'))
    
    user_memberships = Membership.query.filter_by(user_id=user.id).all()
    already_in_team = False
    for mem in user_memberships:
        team = Team.query.filter_by(id=mem.team_id).one()
        if (team.id == team_id):
            already_in_team = True
            break

    if not already_in_team:
        req = Request()
        req.team_id = team_id
        req.user_id = user.id
        db.session.add(req)
        db.session.commit()
        flash("Request made")

    return redirect(url_for('team_page', team_id=team_id))

@scrim_app.route('/team/<team_id>/accept_user/<user_id>')
@login_required
def team_accept_user(team_id, user_id):
    """
    Deletes the request and makes a new memebrship
    """

    try:
        user = Request.query.filter(and_(Request.team_id==team_id, Request.user_id==user_id)).one()
    except NoResultFound, e:
        flash("No user")
        return redirect(url_for('index'))

    db.session.delete(user)
    db.session.commit()

    new_membership = Membership()
    new_membership.user_id = user_id
    new_membership.team_id = team_id
    new_membership.role = "Member"

    db.session.add(new_membership)
    db.session.commit()
    
    flash("Accepted")
    return redirect(url_for('team_page', team_id=team_id))         

@scrim_app.route('/propose/scrim/<int:team_id>', methods=['GET','POST'])
@login_required
def propose_scrim(team_id):
    from wtforms import SelectField

    time_zone = [('CET','CET'),('EST','EST')]
    ProposeScrimForm.time_zone = SelectField('time_zone', choices=time_zone)

    form = ProposeScrimForm()

    if form.validate_on_submit():
        flash('Scrim proposed')
        return redirect(url_for('index'))
    else:
        return render_template('propose_scrim.html', form=form)


@scrim_app.route('/bots/boom')
def bots_boom():
    """
    Make sure stuff works. Let's say no error = It works!

    See bots.py
    """

    from scrim import bots

    print bots

    bots.create_bot_users()
    bots.create_bot_teams()
    bots.make_bot_join_team()

    return 'Trust me. It worked.', 200
