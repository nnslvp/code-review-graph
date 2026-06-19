class UsersController < ApplicationController
  before_action :authenticate

  def index
    @users = User.all
  end

  def show
    @user = User.find(params[:id])
  end

  private

  def authenticate
    head :unauthorized unless current_user
  end
end
