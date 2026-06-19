class EmailJob < ApplicationJob
  def perform(user_id)
    User.find(user_id)
  end
end
