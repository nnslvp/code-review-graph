class User < ApplicationRecord
  has_many :posts, dependent: :destroy
  has_one :profile
  belongs_to :account, class_name: "Org"
  has_and_belongs_to_many :roles
  validates :email, presence: true
  scope :active, -> { where(active: true) }
  before_save :normalize_email

  def normalize_email
    self.email = email.downcase
  end
end
