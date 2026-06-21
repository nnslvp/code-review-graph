module Trackable
  extend ActiveSupport::Concern

  included do
    has_many :modifications, as: :entity
    before_save :store_changes
    scope :recent, -> { order(created_at: :desc) }
  end

  class_methods do
    def tracked?
      true
    end
  end

  def track!
    save
  end
end
