require_relative "./helper"

CONST = 1

module Auth
  MAX = 3

  class Admin::User < ActiveRecord::Base
    include Comparable
    attr_accessor :name
    attribute :age, :integer
    has_many :posts, dependent: :destroy
    belongs_to :account, class_name: "Org"
    scope :active, -> { where(active: true) }

    included do
      before_save :touch
    end

    def self.build(id)
      new(id)
    end

    def call
      save
    end

    class << self
      def helper
        true
      end
    end
  end
end
